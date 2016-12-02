import logging
import colander
import deform
import pystache
from bag.web.pyramid.flash_msg import add_flash
from pyramid.decorator import reify
from pyramid.httpexceptions import HTTPFound, HTTPNotFound
from pyramid.security import remember, forget, Authenticated
from pyramid.settings import asbool
from pyramid.url import route_url

from pyramid_mailer import get_mailer
from pyramid_mailer.message import Message

from .actions import instantiate_action, ActivateUser, CheckCredentials
from .interfaces import (
    IUserClass, IActivationClass, IUIStrings, ILoginForm, ILoginSchema,
    IRegisterForm, IRegisterSchema, IForgotPasswordForm, IForgotPasswordSchema,
    IResetPasswordForm, IResetPasswordSchema, IProfileForm, IProfileSchema)
from .events import (NewRegistrationEvent, RegistrationActivatedEvent,
                     PasswordResetEvent, ProfileUpdatedEvent)
from .exceptions import AuthenticationFailure, FormValidationFailure
from .httpexceptions import HTTPBadRequest
from .models import _


LOG = logging.getLogger(__name__)


def includeme(config):
    """Set up pluserable routes and views in Pyramid."""
    settings = config.registry.settings['pluserable']
    routes = settings['routes']
    for name, kw in routes.items():
        config.add_route(name, **kw)
    for route_name, kw in settings['views'].items():
        if route_name in routes:
            config.add_view(route_name=route_name, **kw)
    if 'login' in routes:
        config.add_view(
            route_name='login', xhr=True, accept="application/json",
            renderer='json', view=AuthView, attr='login_ajax')


def get_config_route(request, config_key):
    settings = request.registry.settings
    try:
        return request.route_url(settings[config_key])
    except KeyError:
        return settings[config_key]


def authenticated(request, userid):
    """Set the auth cookies and redirect.

    ...either to the URL indicated in the "next" parameter,
    or to the page defined in pluserable.login_redirect,
    which defaults to a view named 'index'.
    """
    settings = request.registry.settings
    headers = remember(request, userid)
    autologin = asbool(settings.get('pluserable.autologin', False))

    if not autologin:
        Str = request.registry.getUtility(IUIStrings)
        add_flash(request, plain=Str.authenticated, kind='success')

    location = request.params.get('next') or get_config_route(
        request, 'pluserable.login_redirect')

    return HTTPFound(location=location, headers=headers)


def create_activation(request, user):  # TODO Move to action
    Activation = request.registry.getUtility(IActivationClass)
    activation = Activation()

    repo = request.replusitory
    repo.store_activation(activation)
    user.activation = activation
    repo.flush()

    # TODO Create a hook for the app to give us body and subject!
    # TODO We don't need pystache just for this!
    body = pystache.render(
        _("Please validate your email and activate your account by visiting:\n"
            "{{ link }}"),
        {
            'link': request.route_url('activate', user_id=user.id,
                                      code=user.activation.code)
        }
    )
    subject = _("Please activate your account!")

    message = Message(subject=subject, recipients=[user.email], body=body)
    mailer = get_mailer(request)
    mailer.send(message)


def render_form(request, form, appstruct=None, **kw):
    settings = request.registry.settings
    retail = asbool(settings.get('pluserable.deform_retail', False))

    if appstruct is not None:
        form.set_appstruct(appstruct)

    if not retail:
        form = form.render()

    result = {'form': form}
    result.update(kw)
    return result


def validate_form(controls, form):
    try:
        captured = form.validate(controls)
    except deform.ValidationFailure as e:
        # NOTE(jkoelker) normally this is superfluous, but if the app is
        #                debug logging, then log that we "ate" the exception
        LOG.debug('Form validation failed', exc_info=True)
        raise FormValidationFailure(form, e)
    return captured


class BaseView(object):

    @property
    def request(self):
        # we defined this so that we can override the request in tests easily
        return self._request

    def __init__(self, request):  # TODO REMOVE MOST OF THESE LINES
        self._request = request
        self.settings = request.registry.settings
        getUtility = request.registry.getUtility
        self.User = getUtility(IUserClass)
        self.Activation = getUtility(IActivationClass)
        self.Str = getUtility(IUIStrings)


class AuthView(BaseView):
    def __init__(self, request):
        super(AuthView, self).__init__(request)

        schema = request.registry.getUtility(ILoginSchema)
        self.schema = schema().bind(request=self.request)

        form = request.registry.getUtility(ILoginForm)

        self.login_redirect_view = get_config_route(
            request,
            'pluserable.login_redirect'
        )

        self.logout_redirect_view = get_config_route(
            request,
            'pluserable.logout_redirect'
        )
        self.form = form(self.schema, buttons=(self.Str.login_button,))

    def login_ajax(self):
        try:
            cstruct = self.request.json_body
        except ValueError as e:
            raise HTTPBadRequest({'invalid': str(e)})

        try:
            captured = self.schema.deserialize(cstruct)
        except colander.Invalid as e:
            raise HTTPBadRequest({'invalid': e.asdict()})

        try:
            check_credentials = instantiate_action(
                CheckCredentials, self.request, payload={
                    'handle': captured['handle'],
                    'password': captured['password'],
                })
            user = check_credentials()
        except AuthenticationFailure as e:
            raise HTTPBadRequest({
                'status': 'failure',
                'reason': e.message,
            })

        # We pass the user back as well so the authentication
        # can use its security code or any other information stored
        # on the user
        user_json = user.__json__(self.request)

        return {'status': 'okay', 'user': user_json}

    def login(self):
        if self.request.method == 'GET':
            if self.request.user:
                return HTTPFound(location=self.login_redirect_view)
            return render_form(self.request, self.form)

        elif self.request.method == 'POST':
            controls = self.request.POST.items()
            try:
                captured = validate_form(controls, self.form)
            except FormValidationFailure as e:
                return e.result(self.request)

            try:
                check_credentials = instantiate_action(
                    CheckCredentials, self.request, payload={
                        'handle': captured['handle'],
                        'password': captured['password'],
                    })
                user = check_credentials()
            except AuthenticationFailure as e:
                add_flash(self.request, plain=str(e), kind='error')
                return render_form(self.request, self.form, captured,
                                   errors=[e])

            self.request.user = user
            return authenticated(self.request, user.id_value)

    def logout(self):
        """Remove the auth cookies and redirect...

        ...to the view defined in the ``pluserable.logout_redirect`` setting,
        which defaults to a view named 'index'.
        """
        self.request.session.invalidate()
        headers = forget(self.request)
        add_flash(self.request, plain=self.Str.logout, kind='success')
        return HTTPFound(location=self.logout_redirect_view, headers=headers)


class ForgotPasswordView(BaseView):

    def __init__(self, request):
        super(ForgotPasswordView, self).__init__(request)

        self.forgot_password_redirect_view = route_url(
            self.settings.get('pluserable.forgot_password_redirect', 'index'),
            request)
        self.reset_password_redirect_view = route_url(
            self.settings.get('pluserable.reset_password_redirect', 'index'),
            request)

    def forgot_password(self):
        """Show or process the "forgot password" form."""
        request = self.request
        schema = request.registry.getUtility(IForgotPasswordSchema)
        schema = schema().bind(request=request)

        form = request.registry.getUtility(IForgotPasswordForm)
        form = form(schema)

        if request.method == 'GET':
            if request.user:
                return HTTPFound(location=self.forgot_password_redirect_view)
            else:
                return render_form(request, form)

        # From here on, we know it's a POST. Let's validate the form
        controls = request.POST.items()

        try:
            captured = validate_form(controls, form)
        except FormValidationFailure as e:
            return e.result(request)

        repo = request.replusitory
        user = repo.q_user_by_email(captured['email'])
        activation = self.Activation()
        # dbsession.add(activation)  # seems unnecessary
        user.activation = activation
        repo.flush()  # initialize activation.code

        Str = self.Str

        # TODO: Generate msg in a separate method so subclasses can override
        mailer = get_mailer(request)
        username = getattr(user, 'short_name', '') or \
            getattr(user, 'full_name', '') or \
            getattr(user, 'username', '') or user.email
        body = Str.reset_password_email_body.format(
            link=route_url('reset_password', request, code=activation.code),
            username=username, domain=request.application_url)
        subject = Str.reset_password_email_subject
        message = Message(subject=subject, recipients=[user.email], body=body)
        mailer.send(message)

        add_flash(self.request, plain=Str.reset_password_email_sent,
                  kind='success')
        return HTTPFound(location=self.reset_password_redirect_view)

    def reset_password(self):
        request = self.request
        schema = request.registry.getUtility(IResetPasswordSchema)
        schema = schema().bind(request=request)

        form = request.registry.getUtility(IResetPasswordForm)
        form = form(schema)

        code = request.matchdict.get('code', None)
        activation = request.replusitory.q_activation_by_code(code)
        if not activation:
            return HTTPNotFound()

        user = request.replusitory.q_user_by_activation(activation)

        if request.method == 'GET':
            appstruct = {'username': user.username} if hasattr(
                user, 'username') else {'email': user.email}
            return render_form(request, form, appstruct)

        elif request.method == 'POST':
            controls = request.POST.items()
            try:
                captured = validate_form(controls, form)
            except FormValidationFailure as e:
                return e.result(request)

            password = captured['password']

            user.password = password
            request.replusitory.delete_activation(activation)

            add_flash(request, plain=self.Str.reset_password_done,
                      kind='success')
            request.registry.notify(PasswordResetEvent(
                request, user, password))
            location = self.reset_password_redirect_view
            return HTTPFound(location=location)


class RegisterView(BaseView):

    def __init__(self, request):
        super(RegisterView, self).__init__(request)
        schema = request.registry.getUtility(IRegisterSchema)
        self.schema = schema().bind(request=self.request)

        form = request.registry.getUtility(IRegisterForm)
        self.form = form(self.schema)

        self.after_register_url = route_url(
            self.settings.get('pluserable.register_redirect', 'index'),
            request)
        self.after_activate_url = route_url(
            self.settings.get('pluserable.activate_redirect', 'index'),
            request)

        self.require_activation = asbool(
            self.settings.get('pluserable.require_activation', True))

        if self.require_activation:
            self.mailer = get_mailer(request)

    def register(self):
        if self.request.method == 'GET':
            if self.request.user:
                return HTTPFound(location=self.after_register_url)

            return render_form(self.request, self.form)

        elif self.request.method != 'POST':
            return

        # If the request is a POST:
        controls = self.request.POST.items()

        try:
            captured = validate_form(controls, self.form)
        except FormValidationFailure as e:
            return e.result(self.request)

        # With the form validated, we know email and username are unique.
        user = self.persist_user(captured)

        autologin = asbool(self.settings.get('pluserable.autologin', False))

        if self.require_activation:
            # SEND EMAIL ACTIVATION
            create_activation(self.request, user)
            add_flash(self.request, plain=self.Str.activation_check_email,
                      kind='success')
        elif not autologin:
            add_flash(self.request, plain=self.Str.registration_done,
                      kind='success')

        self.request.registry.notify(NewRegistrationEvent(
            self.request, user, None, controls))
        if autologin:
            self.request.replusitory.flush()  # in order to get the id
            return authenticated(self.request, user.id)
        else:  # not autologin: user must log in just after registering.
            return HTTPFound(location=self.after_register_url)

    def persist_user(self, controls):
        """To change how the user is stored, override this method."""
        # This generic method must work with any custom User class and any
        # custom registration form:
        user = self.User(**controls)
        self.request.replusitory.store_user(user)
        return user

    def activate(self):
        activate_user = instantiate_action(
            ActivateUser, self.request, payload={
                'code':    self.request.matchdict.get('code', None),
                'user_id': self.request.matchdict.get('user_id', None),
            })
        user, activation = activate_user()

        add_flash(self.request, plain=self.Str.activation_email_verified,
                  kind='success')
        self.request.registry.notify(
            RegistrationActivatedEvent(self.request, user, activation))
        # If an exception is raised in an event subscriber, this never runs:
        return HTTPFound(location=self.after_activate_url)


class ProfileView(BaseView):

    def profile(self):
        user_id = self.request.matchdict.get('user_id', None)
        user = self.request.replusitory.q_user_by_id(user_id)
        if not user:
            return HTTPNotFound()
        return {'user': user}

    def _get_form(self):
        schema = self.request.registry.getUtility(IProfileSchema)
        self.schema = schema().bind(request=self.request)

        form = self.request.registry.getUtility(IProfileForm)
        return form(self.schema)

    def edit_profile(self):  # TODO TEST OR REMOVE
        user = self.request.user
        # if not user:  # substitute with effective_principals=Authenticated
        #     return HTTPNotFound()

        form = self._get_form()

        if self.request.method == 'GET':
            appstruct = {'email': user.email or ''}
            if hasattr(user, 'username'):
                appstruct['username'] = user.username
            return render_form(self.request, form, appstruct)

        elif self.request.method == 'POST':
            controls = self.request.POST.items()

            try:
                captured = validate_form(controls, form)
            except FormValidationFailure as e:
                if hasattr(user, 'username'):
                    # We pre-populate username
                    return e.result(self.request, username=user.username)
                else:
                    return e.result(self.request)

            changed = False
            email = captured.get('email', None)
            if email:
                email_user = self.User.get_by_email(self.request, email)
                if email_user and email_user.id != user.id:
                    # TODO This should be a validation error, not add_flash
                    add_flash(
                        self.request,
                        plain=self.Str.edit_profile_email_present.format(
                            email=email),
                        kind='error')
                    return HTTPFound(location=self.request.url)
                if email != user.email:
                    user.email = email
                    changed = True

            password = captured.get('password')
            if password:
                user.password = password
                changed = True

            if changed:
                add_flash(self.request, plain=self.Str.edit_profile_done,
                          kind='success')
                self.request.registry.notify(
                    ProfileUpdatedEvent(self.request, user, captured)
                )
            return HTTPFound(location=self.request.url)


def get_pyramid_views_config():
    """A dictionary for registering Pyramid views."""
    return {  # route_name: view_kwargs
        'register': {'view': RegisterView, 'attr': 'register',
                     'renderer': 'pluserable:templates/register.mako'},
        'activate': {'view': RegisterView, 'attr': 'activate'},
        'login': {'view': AuthView, 'attr': 'login',
                  'renderer': 'pluserable:templates/login.mako'},
        'logout': {'permission': 'view',
                   'view': AuthView, 'attr': 'logout'},
        'forgot_password': {
            'view': ForgotPasswordView, 'attr': 'forgot_password',
            'renderer': 'pluserable:templates/forgot_password.mako'},
        'reset_password': {
            'view': ForgotPasswordView, 'attr': 'reset_password',
            'renderer': 'pluserable:templates/reset_password.mako'},
        'profile': {'view': ProfileView, 'attr': 'profile',
                    'renderer': 'pluserable:templates/profile.mako'},
        'edit_profile': {'view': ProfileView, 'attr': 'edit_profile',
                         'effective_principals': Authenticated,
                         'renderer': 'pluserable:templates/edit_profile.mako'},
    }

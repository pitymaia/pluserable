"""Base model classes for any backend (SQLAlchemy, ZODB etc.)."""

# TODO: How is a unique "column" enforced in ZODB?

from datetime import datetime, timedelta
import hashlib
from urllib.parse import urlencode

from bag.text import pluralize
from bag.text.hash import random_hash

import cryptacular.bcrypt

crypt = cryptacular.bcrypt.BCRYPTPasswordManager()


def three_days_from_now():
    return datetime.utcnow() + timedelta(days=3)


class ActivationBase:
    """Handles activations and password reset items for users.

    ``code`` is a random hash that is valid only once.
    Once the hash is used to access the site, it is removed.

    ``valid_until`` is a datetime until when the activation key will last.

    ``created_by`` is a system: new user registration, password reset,
    forgot password etc.
    """

    def __init__(self, code=None, valid_until=None, created_by='web'):
        """Usually call with the ``created_by`` system, or no arguments."""
        self.code = code or random_hash()
        self.valid_until = valid_until or three_days_from_now()
        assert isinstance(self.valid_until, datetime)
        self.created_by = created_by


class UserBase:
    """Base class for a User model."""

    def __init__(self, email, password, salt=None, activation=None, **kw):
        """User constructor."""
        # print('User constructor: {} / {} / {} / {}'.format(
        #     email, password, salt, activation))
        self.email = email
        assert self.email and isinstance(self.email, str)
        self.salt = self.salt or random_hash(24)
        self.password = password
        assert self.password and isinstance(self.password, str)
        self.activation = activation
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self):
        return '<User: %s>' % self.email

    def gravatar_url(self, default='mm', size=80, cacheable=True):
        """Return a Gravatar image URL for this user."""
        base = "http://www.gravatar.com/avatar/" if cacheable else \
            "https://secure.gravatar.com/avatar/"
        return base + \
            hashlib.md5(self.email.encode('utf8').lower()).hexdigest() + \
            "?" + urlencode({'d': default, 's': str(size)})

    @property
    def password(self):
        """Set the password, or retrieve the password hash."""
        return self._password

    @password.setter
    def password(self, value):
        self._password = self._hash_password(value)

    def _hash_password(self, password):
        assert self.salt, "UserBase constructor was not called; " \
            "you probably have your User base classes in the wrong order."
        return str(crypt.encode(password + self.salt))

    @classmethod
    def generate_random_password(cls, chars=12):
        """Generate random string of fixed length."""
        return random_hash(chars)

    def check_password(self, password):
        """Check the ``password`` and return a boolean."""
        if not password:
            return False
        return crypt.check(self.password, password + self.salt)

    @property
    def is_activated(self):
        """False if this user needs to confirm her email address."""
        return self.activation is None

    # @property
    # def __acl__(self):
    #     return [
    #         (Allow, 'user:%s' % self.id, 'access_user')
    #     ]


class GroupBase:
    """Base class for a Group model."""

    def __init__(self, name, description=None, users=[]):
        """Constructor."""
        assert name and isinstance(name, str)
        self.name = name
        self.description = description
        self.users = users

    def __repr__(self):
        return '<Group: {}>'.format(self.name)

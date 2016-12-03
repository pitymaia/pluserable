"""Unit tests for pure methods of models."""

from datetime import datetime
from pluserable.tests.models import Activation, Base, Group
from . import FastTestCase


class SomeModel(Base):
    pass


class TestBaseModel(FastTestCase):

    def test_tablename(self):
        model = SomeModel()
        assert model.__tablename__ == 'some_model'


class TestActivation(FastTestCase):

    def test_create_activation_with_valid_until(self):
        dt = datetime.utcnow()
        activation1 = Activation()
        activation1.valid_until = dt
        assert activation1.valid_until == dt


class TestGroup(FastTestCase):

    def test_constructor(self):
        group = Group(name='foo', description='bar')
        assert group.name == 'foo'
        assert group.description == 'bar'
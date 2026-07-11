"""Production must not fall back to built-in defaults for encryption secrets.

`DEFF_SALT` / `DEFF_PASSWORD` key the encrypted document storage. Base/Dev/Test
carry placeholder defaults so local dev and CI work, but the `Prod`
configuration should require them to be set in the environment and fail fast
otherwise, rather than silently keying encryption with a non-secret default.
"""

import os
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from fighthealthinsurance.settings import Base, Dev, Prod, _require_prod_secret


class RequireProdSecretTest(SimpleTestCase):
    def test_raises_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOME_SECRET", None)
            with self.assertRaises(ImproperlyConfigured):
                _require_prod_secret("SOME_SECRET")

    def test_raises_when_env_empty(self):
        with patch.dict(os.environ, {"SOME_SECRET": ""}):
            with self.assertRaises(ImproperlyConfigured):
                _require_prod_secret("SOME_SECRET")

    def test_returns_value_when_set(self):
        with patch.dict(os.environ, {"SOME_SECRET": "real-value"}):
            self.assertEqual(_require_prod_secret("SOME_SECRET"), "real-value")


class ProdEncryptionSecretsTest(SimpleTestCase):
    def _prod_getter(self, name):
        # Grab the property descriptor without going through
        # django-configurations, and return its getter (which ignores self).
        prop = Prod.__dict__[name]
        assert isinstance(prop, property)
        return prop.fget

    def test_prod_raises_when_deff_secrets_missing(self):
        salt = self._prod_getter("DEFF_SALT")
        password = self._prod_getter("DEFF_PASSWORD")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFF_SALT", None)
            os.environ.pop("DEFF_PASSWORD", None)
            with self.assertRaises(ImproperlyConfigured):
                salt(None)
            with self.assertRaises(ImproperlyConfigured):
                password(None)

    def test_prod_uses_env_values_when_set(self):
        salt = self._prod_getter("DEFF_SALT")
        password = self._prod_getter("DEFF_PASSWORD")
        with patch.dict(
            os.environ, {"DEFF_SALT": "real-salt", "DEFF_PASSWORD": "real-pw"}
        ):
            self.assertEqual(salt(None), "real-salt")
            self.assertEqual(password(None), "real-pw")

    def test_only_prod_requires_env(self):
        # Non-prod configs keep a plain-string fallback so dev/CI never raise.
        self.assertIsInstance(Prod.__dict__["DEFF_SALT"], property)
        self.assertIsInstance(Prod.__dict__["DEFF_PASSWORD"], property)
        self.assertNotIsInstance(Base.__dict__["DEFF_SALT"], property)
        self.assertNotIsInstance(Dev.__dict__["DEFF_SALT"], property)

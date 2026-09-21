import os
from unittest import TestCase
from unittest.mock import patch

from config.env_resolution import DjangoEnvError, resolve_django_env


class ResolveDjangoEnvTests(TestCase):
    def test_dev_convenience_defaults_to_local_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DJANGO_ENV", None)
            self.assertEqual(resolve_django_env(fail_closed=False), "local")

    def test_dev_convenience_honors_explicit_value(self):
        with patch.dict(os.environ, {"DJANGO_ENV": "uat"}):
            self.assertEqual(resolve_django_env(fail_closed=False), "uat")

    def test_fail_closed_raises_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DJANGO_ENV", None)
            with self.assertRaises(DjangoEnvError):
                resolve_django_env(fail_closed=True)

    def test_fail_closed_raises_on_unrecognized_value(self):
        with patch.dict(os.environ, {"DJANGO_ENV": "bogus"}):
            with self.assertRaises(DjangoEnvError):
                resolve_django_env(fail_closed=True)

    def test_fail_closed_accepts_known_values(self):
        for env in ("local", "staging", "uat", "production"):
            with patch.dict(os.environ, {"DJANGO_ENV": env}):
                self.assertEqual(resolve_django_env(fail_closed=True), env)


class StagingSettingsModuleTests(TestCase):
    def test_staging_settings_imports_base_and_disables_debug(self):
        import importlib
        staging = importlib.import_module("config.settings.staging")
        importlib.reload(staging)  # ensure a fresh import, not a cached empty module
        self.assertFalse(staging.DEBUG)
        self.assertTrue(hasattr(staging, "SECRET_KEY"))
        self.assertTrue(hasattr(staging, "DATABASES"))
        self.assertTrue(hasattr(staging, "INSTALLED_APPS"))
        self.assertIn("apps.orders", staging.INSTALLED_APPS)
        self.assertTrue(hasattr(staging, "ALLOWED_HOSTS"))
        self.assertTrue(hasattr(staging, "CORS_ALLOWED_ORIGINS"))

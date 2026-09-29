import importlib
import os
import sys
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings.base import redis_cache, require_env


class SettingsHelperTests(SimpleTestCase):

    def test_require_env_returns_value(self):
        with patch.dict("os.environ", {"SMF_TEST_VAR": "redis://x:6379/0"}):
            self.assertEqual(require_env("SMF_TEST_VAR"), "redis://x:6379/0")

    def test_require_env_missing_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                require_env("SMF_TEST_VAR")

    def test_require_env_blank_raises(self):
        with patch.dict("os.environ", {"SMF_TEST_VAR": "  "}):
            with self.assertRaises(ImproperlyConfigured):
                require_env("SMF_TEST_VAR")

    def test_redis_cache_structure(self):
        default = redis_cache("redis://cache:6379/1")["default"]
        self.assertEqual(default["BACKEND"], "django.core.cache.backends.redis.RedisCache")
        self.assertEqual(default["LOCATION"], "redis://cache:6379/1")
        self.assertEqual(default["KEY_PREFIX"], "smerfume")
        self.assertEqual(default["OPTIONS"], {"socket_connect_timeout": 0.5, "socket_timeout": 0.5})

    def test_default_rate_settings(self):
        self.assertEqual(settings.OTP_MAX_SENDS_PER_MOBILE_PER_HOUR, 5)
        self.assertEqual(settings.REST_FRAMEWORK["NUM_PROXIES"], 0)


class DeployedSettingsFailFastTests(SimpleTestCase):
    """Import each deployed settings module in isolation (never activated;
    django.conf.settings is untouched) with required variables removed."""

    DEPLOYED = ("staging", "uat", "production")
    VALID = {"REDIS_URL": "redis://cache:6379/0", "NUM_PROXIES": "1"}

    def _import(self, name, env):
        # base is re-executed too, so it reads the patched environment.
        module_name = f"config.settings.{name}"
        base = sys.modules["config.settings.base"]
        clean = {k: v for k, v in os.environ.items() if k not in self.VALID}
        with patch.dict("os.environ", {**clean, **env}, clear=True):
            sys.modules.pop(module_name, None)
            sys.modules.pop("config.settings.base", None)
            try:
                return importlib.import_module(module_name)
            finally:
                sys.modules.pop(module_name, None)
                sys.modules["config.settings.base"] = base

    def test_missing_redis_url_refuses_to_start(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, "REDIS_URL"):
                self._import(name, {"NUM_PROXIES": "1"})

    def test_missing_num_proxies_refuses_to_start(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, "NUM_PROXIES"):
                self._import(name, {"REDIS_URL": "redis://cache:6379/0"})

    def test_blank_values_refuse_to_start(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaises(ImproperlyConfigured):
                self._import(name, {"REDIS_URL": "", "NUM_PROXIES": ""})

    def test_valid_config_uses_redis_and_proxy_count(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                module = self._import(name, self.VALID)
                self.assertEqual(
                    module.CACHES["default"]["BACKEND"],
                    "django.core.cache.backends.redis.RedisCache",
                )
                self.assertEqual(module.REST_FRAMEWORK["NUM_PROXIES"], 1)

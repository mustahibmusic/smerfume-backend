import importlib
import os
import sys
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase
from redis.exceptions import RedisError
from rest_framework.test import APIClient

from config.settings.base import database_config, parse_num_proxies, redis_cache, require_env


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

    def test_redis_cache_accepts_supported_urls(self):
        for url in (
            "redis://cache:6379/0",
            "rediss://user:secret@cache.example.com:6380/1",
            "unix:///var/run/redis.sock",
        ):
            with self.subTest(url):
                self.assertEqual(redis_cache(url)["default"]["LOCATION"], url)

    def test_redis_cache_rejects_malformed_urls(self):
        for url in (
            "cache:6379",
            "http://cache:6379/0",
            "redis://",
            "redis://:6379/0",
            "redis://cache:port/0",
            "redis://cache:6379/db",
            "unix://",
        ):
            with self.subTest(url), self.assertRaises(ImproperlyConfigured):
                redis_cache(url)

    def test_redis_cache_error_hides_credentials(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            redis_cache("http://user:secret@cache:6379/0")
        self.assertNotIn("secret", str(ctx.exception))

    def test_parse_num_proxies_accepts_non_negative_integers(self):
        for raw, expected in (("0", 0), ("1", 1), (" 2 ", 2)):
            with self.subTest(raw):
                self.assertEqual(parse_num_proxies(raw), expected)

    def test_parse_num_proxies_rejects_invalid_values(self):
        for raw in ("-1", "abc", "1.5", "one", "+-1"):
            with self.subTest(raw), self.assertRaises(ImproperlyConfigured):
                parse_num_proxies(raw)

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

    def test_invalid_values_refuse_to_start(self):
        for name in self.DEPLOYED:
            for env, needle in (
                ({"REDIS_URL": "redis://cache:6379/0", "NUM_PROXIES": "-1"}, "NUM_PROXIES"),
                ({"REDIS_URL": "redis://cache:6379/0", "NUM_PROXIES": "abc"}, "NUM_PROXIES"),
                ({"REDIS_URL": "cache:6379", "NUM_PROXIES": "1"}, "REDIS_URL"),
            ):
                with self.subTest(name=name, env=env),                         self.assertRaisesMessage(ImproperlyConfigured, needle):
                    self._import(name, env)

    def test_valid_config_uses_redis_and_proxy_count(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                module = self._import(name, self.VALID)
                self.assertEqual(
                    module.CACHES["default"]["BACKEND"],
                    "django.core.cache.backends.redis.RedisCache",
                )
                self.assertEqual(module.REST_FRAMEWORK["NUM_PROXIES"], 1)


def _broken_cache():
    broken = MagicMock()
    broken.get.side_effect = RedisError("connection refused")
    broken.set.side_effect = RedisError("connection refused")
    return broken


class FailOpenThrottleTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_catalogue_stays_up_when_redis_is_down(self):
        from apps.core.throttling import FailOpenAnonRateThrottle

        with patch.object(FailOpenAnonRateThrottle, "cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = APIClient().get("/api/catalog/products/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("scope=anon", logs.output[0])
        self.assertNotIn("connection refused", resp.content.decode())



class DatabaseConfigTests(SimpleTestCase):
    """DATABASE_URL (any provider) with the DB_* variables as the local fallback."""

    DB_VARS = ("DATABASE_URL", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_SSLMODE", "CONN_MAX_AGE")

    def _config(self, env, **kwargs):
        clean = {k: v for k, v in os.environ.items() if k not in self.DB_VARS}
        with patch.dict("os.environ", {**clean, **env}, clear=True):
            return database_config(**kwargs)

    def test_database_url_with_encoded_credentials_and_sslmode(self):
        db = self._config({"DATABASE_URL": "postgres://us%40er:p%2Fss%3Aword@db.example.com:6543/shop?sslmode=require"})
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual((db["USER"], db["PASSWORD"], db["HOST"], db["PORT"], db["NAME"]),
                         ("us@er", "p/ss:word", "db.example.com", 6543, "shop"))
        self.assertEqual(db["OPTIONS"]["sslmode"], "require")
        self.assertTrue(db["CONN_HEALTH_CHECKS"])

    def test_postgresql_scheme_and_default_port(self):
        db = self._config({"DATABASE_URL": "postgresql://u:p@db.example.com/shop"})
        self.assertEqual((db["HOST"], db["NAME"]), ("db.example.com", "shop"))
        self.assertIn(db["PORT"], ("", None, 5432, "5432"))

    def test_db_vars_fallback_when_no_url(self):
        db = self._config({"DB_NAME": "smerfume", "DB_USER": "dev", "DB_PASSWORD": "pw",
                           "DB_HOST": "localhost", "DB_PORT": "5433"})
        self.assertEqual((db["ENGINE"], db["NAME"], db["USER"], db["PASSWORD"], db["HOST"], db["PORT"]),
                         ("django.db.backends.postgresql", "smerfume", "dev", "pw", "localhost", "5433"))
        self.assertTrue(db["CONN_HEALTH_CHECKS"])
        self.assertNotIn("sslmode", db.get("OPTIONS", {}))

    def test_local_prefers_db_vars_over_a_stray_url(self):
        env = {"DATABASE_URL": "postgres://u:p@elsewhere/otherdb", "DB_NAME": "smerfume"}
        self.assertEqual(self._config(env, prefer_url=False)["NAME"], "smerfume")
        self.assertEqual(self._config(env)["NAME"], "otherdb")
        self.assertEqual(self._config({"DATABASE_URL": "postgres://u:p@h/only"}, prefer_url=False)["NAME"], "only")

    def test_blank_url_uses_fallback(self):
        db = self._config({"DATABASE_URL": "  ", "DB_NAME": "smerfume"})
        self.assertEqual(db["NAME"], "smerfume")

    def test_conn_max_age_env_and_default(self):
        self.assertEqual(self._config({"DB_NAME": "x"})["CONN_MAX_AGE"], 0)
        self.assertEqual(self._config({"DB_NAME": "x"}, default_conn_max_age=60)["CONN_MAX_AGE"], 60)
        self.assertEqual(self._config({"DB_NAME": "x", "CONN_MAX_AGE": "30"}, default_conn_max_age=60)["CONN_MAX_AGE"], 30)
        with self.assertRaises(ImproperlyConfigured):
            self._config({"DB_NAME": "x", "CONN_MAX_AGE": "soon"})

    def test_db_sslmode_applies_to_both_paths(self):
        db = self._config({"DB_NAME": "x", "DB_SSLMODE": "require"})
        self.assertEqual(db["OPTIONS"]["sslmode"], "require")
        db = self._config({"DATABASE_URL": "postgres://u:p@h/db", "DB_SSLMODE": "verify-full"})
        self.assertEqual(db["OPTIONS"]["sslmode"], "verify-full")

    def test_non_postgres_url_refused_without_echoing_it(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._config({"DATABASE_URL": "mysql://user:secret@h/db"})
        self.assertNotIn("secret", str(ctx.exception))

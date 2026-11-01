import importlib
import io
import logging
import logging.config
import os
import sys
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import path
from django.utils.log import configure_logging
from redis.exceptions import RedisError
from rest_framework.test import APIClient

from config.settings.base import (
    database_config, logging_config, object_storage_settings, parse_num_proxies, redis_cache, require_env,
)


S3_ENV = {
    "USE_S3": "True",
    "AWS_ACCESS_KEY_ID": "test-key-id",
    "AWS_SECRET_ACCESS_KEY": "test-secret",
    "AWS_STORAGE_BUCKET_NAME": "smerfume-public",
    "AWS_PRIVATE_STORAGE_BUCKET_NAME": "smerfume-private",
    "AWS_S3_ENDPOINT_URL": "https://account.r2.example.com",
    "AWS_S3_CUSTOM_DOMAIN": "media.example.com",
    "AWS_S3_REGION_NAME": "",
    "AWS_S3_ADDRESSING_STYLE": "",
}


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
    # Required everywhere deployed; tests about Redis/proxies get them for free.
    BASELINE = {
        "SECRET_KEY": "test-secret", "ALLOWED_HOSTS": "api.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://api.example.com",
    }
    MANAGED = (
        "REDIS_URL", "NUM_PROXIES", "SECRET_KEY", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "UAT_HOST",
        "TRUST_PROXY_SSL_HEADER", "SECURE_HSTS_SECONDS", "DATABASE_URL", "CONN_MAX_AGE", "LOG_LEVEL",
        "USE_S3", *S3_ENV,
    )

    def _import(self, name, env):
        # base is re-executed too, so it reads the patched environment.
        module_name = f"config.settings.{name}"
        base = sys.modules["config.settings.base"]
        clean = {k: v for k, v in os.environ.items() if k not in self.MANAGED}
        with patch.dict("os.environ", {**clean, **self.BASELINE, **env}, clear=True):
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

    # Secrets, hosts and origins -------------------------------------------

    def test_secret_key_required(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, "SECRET_KEY"):
                self._import(name, {**self.VALID, "SECRET_KEY": ""})

    def test_allowed_hosts_required_and_parsed(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, "ALLOWED_HOSTS"):
                self._import(name, {**self.VALID, "ALLOWED_HOSTS": " , "})
            module = self._import(name, {**self.VALID, "ALLOWED_HOSTS": "api.example.com, admin.example.com"})
            self.assertEqual(module.ALLOWED_HOSTS, ["api.example.com", "admin.example.com"])

    def test_uat_keeps_uat_host_fallback(self):
        module = self._import("uat", {**self.VALID, "ALLOWED_HOSTS": "", "UAT_HOST": "uat.example.com"})
        self.assertEqual(module.ALLOWED_HOSTS, ["uat.example.com"])

    def test_production_requires_csrf_trusted_origins(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "CSRF_TRUSTED_ORIGINS"):
            self._import("production", {**self.VALID, "CSRF_TRUSTED_ORIGINS": ""})
        module = self._import("production", self.VALID)
        self.assertEqual(module.CSRF_TRUSTED_ORIGINS, ["https://api.example.com"])

    # Proxy / HTTPS ---------------------------------------------------------

    def test_proxy_ssl_header_only_when_explicitly_trusted(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                self.assertIsNone(getattr(self._import(name, self.VALID), "SECURE_PROXY_SSL_HEADER", None))
                module = self._import(name, {**self.VALID, "TRUST_PROXY_SSL_HEADER": "True"})
                self.assertEqual(module.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))
                other = self._import(name, {**self.VALID, "TRUST_PROXY_SSL_HEADER": "yes"})
                self.assertIsNone(getattr(other, "SECURE_PROXY_SSL_HEADER", None))

    def test_initial_hsts_is_short_without_subdomains_or_preload(self):
        for name in ("staging", "production"):
            with self.subTest(name):
                module = self._import(name, self.VALID)
                self.assertTrue(module.SECURE_SSL_REDIRECT)
                self.assertEqual(module.SECURE_HSTS_SECONDS, 300)
                self.assertFalse(module.SECURE_HSTS_INCLUDE_SUBDOMAINS)
                self.assertFalse(getattr(module, "SECURE_HSTS_PRELOAD", False))
                longer = self._import(name, {**self.VALID, "SECURE_HSTS_SECONDS": "86400"})
                self.assertEqual(longer.SECURE_HSTS_SECONDS, 86400)
                with self.assertRaisesMessage(ImproperlyConfigured, "SECURE_HSTS_SECONDS"):
                    self._import(name, {**self.VALID, "SECURE_HSTS_SECONDS": "a year"})

    def test_deployed_database_prefers_url_with_persistent_connections(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                module = self._import(name, {**self.VALID, "DATABASE_URL": "postgres://u:p@db.example.com:5432/shop"})
                db = module.DATABASES["default"]
                self.assertEqual((db["HOST"], db["NAME"], db["CONN_MAX_AGE"]), ("db.example.com", "shop", 60))
                self.assertTrue(db["CONN_HEALTH_CHECKS"])

    def test_deployed_logging_goes_to_stdout_and_stderr(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                module = self._import(name, {**self.VALID, "LOG_LEVEL": "warning"})
                handlers = module.LOGGING["handlers"]
                self.assertEqual(handlers["stdout"]["stream"], "ext://sys.stdout")
                self.assertEqual(handlers["stderr"]["stream"], "ext://sys.stderr")
                self.assertEqual(module.LOGGING["root"]["level"], "WARNING")
                with self.assertRaisesMessage(ImproperlyConfigured, "LOG_LEVEL"):
                    self._import(name, {**self.VALID, "LOG_LEVEL": "loud"})

    def test_s3_storage_uses_separate_public_and_private_backends(self):
        env = {**self.VALID, **S3_ENV}
        for name in self.DEPLOYED:
            with self.subTest(name):
                module = self._import(name, env)
                self.assertEqual(module.STORAGES["default"]["BACKEND"], "apps.core.storage.PublicMediaStorage")
                self.assertEqual(module.STORAGES["private_documents"]["BACKEND"],
                                 "apps.core.storage.PrivateMediaStorage")
                self.assertEqual(module.MEDIA_URL, "https://media.example.com/")

    def test_invalid_s3_configuration_refuses_to_start(self):
        for name in self.DEPLOYED:
            for change, needle in (
                ({"AWS_PRIVATE_STORAGE_BUCKET_NAME": ""}, "AWS_PRIVATE_STORAGE_BUCKET_NAME"),
                ({"AWS_PRIVATE_STORAGE_BUCKET_NAME": "SMERFUME-PUBLIC"}, "different"),
            ):
                with self.subTest(name=name, change=change), self.assertRaisesMessage(ImproperlyConfigured, needle):
                    self._import(name, {**self.VALID, **S3_ENV, **change})

    def test_api_docs_not_public_when_deployed(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                self.assertFalse(self._import(name, self.VALID).API_DOCS_PUBLIC)


@override_settings(
    SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    ALLOWED_HOSTS=["api.example.com"],
)
class TrustedProxyRedirectTests(TestCase):
    """Behind a TLS-terminating proxy the app sees plain HTTP; trusting the
    proxy's X-Forwarded-Proto header is what prevents a redirect loop."""

    URL = "/admin/login/"

    def test_forwarded_https_is_served_not_redirected(self):
        resp = self.client.get(self.URL, HTTP_HOST="api.example.com", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(resp.status_code, 200)

    def test_plain_http_is_redirected_once_to_https(self):
        resp = self.client.get(self.URL, HTTP_HOST="api.example.com")
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "https://api.example.com/admin/login/")

    @override_settings(SECURE_PROXY_SSL_HEADER=None)
    def test_untrusted_header_is_ignored(self):
        resp = self.client.get(self.URL, HTTP_HOST="api.example.com", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(resp.status_code, 301)


class ApiDocsAccessTests(TestCase):
    """The OpenAPI schema and doc pages are staff-only outside local."""

    URLS = ("/api/schema/", "/api/docs/", "/api/redoc/")

    def setUp(self):
        from django.contrib.auth import get_user_model
        users = get_user_model()
        self.staff = users.objects.create_user(
            username="docstaff", email="docstaff@example.com", password="x", is_staff=True,
        )
        self.customer = users.objects.create_user(username="doccust", email="doccust@example.com", password="x")

    def _statuses(self, user=None):
        if user:
            self.client.force_login(user)
        else:
            self.client.logout()
        return [self.client.get(url).status_code for url in self.URLS]

    @override_settings(API_DOCS_PUBLIC=False)
    def test_staff_only_outside_local(self):
        for status_code in self._statuses():
            self.assertIn(status_code, (401, 403))
        self.assertEqual(self._statuses(self.customer), [403, 403, 403])
        self.assertEqual(self._statuses(self.staff), [200, 200, 200])

    @override_settings(API_DOCS_PUBLIC=False)
    def test_inactive_staff_refused(self):
        self.staff.is_active = False
        self.staff.save()
        for status_code in self._statuses(self.staff):
            self.assertIn(status_code, (401, 403))

    @override_settings(API_DOCS_PUBLIC=True)
    def test_open_in_local_development(self):
        self.assertEqual(self._statuses(), [200, 200, 200])

    def test_local_settings_keep_docs_open(self):
        self.assertTrue(settings.API_DOCS_PUBLIC)


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



def _boom(request):
    raise RuntimeError("internal detail 7f3a")


urlpatterns = [path("boom/", _boom)]


@override_settings(ROOT_URLCONF="apps.core.tests", DEBUG=False)
class StdoutLoggingTests(SimpleTestCase):
    """Unhandled 500s reach the provider's log stream with a traceback;
    the HTTP response never carries it."""

    def _with_logging(self, level="INFO"):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            logging.config.dictConfig(logging_config(level))
        return out, err

    def tearDown(self):
        configure_logging(settings.LOGGING_CONFIG, settings.LOGGING)

    def test_unhandled_error_logs_traceback_to_stderr_only(self):
        out, err = self._with_logging()
        client = self.client_class(raise_request_exception=False)
        resp = client.get("/boom/")
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("internal detail 7f3a", resp.content.decode())
        self.assertNotIn("Traceback", resp.content.decode())
        self.assertIn("Traceback", err.getvalue())
        self.assertIn("internal detail 7f3a", err.getvalue())
        self.assertNotIn("internal detail 7f3a", out.getvalue())

    def test_info_goes_to_stdout_not_stderr(self):
        out, err = self._with_logging()
        logging.getLogger("apps.core").info("hello stdout")
        self.assertIn("hello stdout", out.getvalue())
        self.assertNotIn("hello stdout", err.getvalue())

    def test_level_is_respected(self):
        out, err = self._with_logging("WARNING")
        logging.getLogger("apps.core").info("quiet info")
        self.assertNotIn("quiet info", out.getvalue() + err.getvalue())

    def test_no_mail_admins_handler(self):
        config = logging_config("INFO")
        self.assertNotIn("mail_admins", config["handlers"])
        self.assertEqual(config["loggers"]["django"]["handlers"], ["stdout", "stderr"])



@override_settings(ALLOWED_HOSTS=["api.example.com"], SECURE_SSL_REDIRECT=True)
class LivenessTests(TestCase):
    """/healthz/ answers before host and HTTPS handling so a provider's
    internal probe (IP Host header, plain HTTP) works. Only that exact
    path and GET/HEAD get the bypass, and it touches nothing."""

    def test_ok_for_internal_probe_without_db_or_cache(self):
        broken = MagicMock(side_effect=AssertionError("cache touched"))
        with self.assertNumQueries(0), patch.object(cache, "get", broken), patch.object(cache, "set", broken):
            resp = self.client.get("/healthz/", HTTP_HOST="10.0.0.7:8000")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")
        self.assertEqual(resp["Content-Type"], "text/plain")
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_head_allowed(self):
        self.assertEqual(self.client.head("/healthz/", HTTP_HOST="10.0.0.7").status_code, 200)

    def test_no_build_or_environment_details(self):
        resp = self.client.get("/healthz/?verbose=1", HTTP_HOST="10.0.0.7")
        self.assertEqual(resp.content, b"ok")
        for header in ("Server", "X-Version", "X-Environment"):
            self.assertNotIn(header, resp)

    def test_only_the_exact_path_bypasses(self):
        for path in ("/healthz", "/healthz/x", "/HEALTHZ/", "/healthz//", "/api/healthz/", "/admin/"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path, HTTP_HOST="10.0.0.7").status_code, 400)

    def test_other_methods_do_not_bypass(self):
        for method in ("post", "put", "delete", "options"):
            with self.subTest(method):
                resp = getattr(self.client, method)("/healthz/", HTTP_HOST="10.0.0.7")
                self.assertEqual(resp.status_code, 400)

    def test_middleware_runs_first(self):
        self.assertEqual(settings.MIDDLEWARE[0], "apps.core.health.LivenessMiddleware")


@override_settings(ALLOWED_HOSTS=["api.example.com"])
class ReadinessTests(TestCase):
    """/readyz/ goes through normal request handling and checks only the
    primary database (SELECT 1); Redis is not a readiness dependency."""

    def _get(self, **extra):
        return self.client.get("/readyz/", HTTP_HOST="api.example.com", **extra)

    def test_ready_when_database_answers(self):
        broken = MagicMock(side_effect=AssertionError("cache touched"))
        with self.assertNumQueries(1), patch.object(cache, "get", broken):
            resp = self._get()
        self.assertEqual((resp.status_code, resp.content), (200, b"ok"))
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_unavailable_without_detail_when_database_fails(self):
        from django.db import OperationalError

        with patch("apps.core.views.connection.cursor", side_effect=OperationalError("password authentication failed for db-host")), \
                self.assertLogs("apps.core.views", "ERROR"):
            resp = self._get()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.content, b"unavailable")
        self.assertNotIn(b"password", resp.content)

    def test_normal_host_validation_applies(self):
        self.assertEqual(self.client.get("/readyz/", HTTP_HOST="10.0.0.7").status_code, 400)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_normal_https_handling_applies(self):
        self.assertEqual(self._get().status_code, 301)

    def test_no_login_needed_and_get_only(self):
        self.assertEqual(self._get().status_code, 200)
        self.assertEqual(self.client.post("/readyz/", HTTP_HOST="api.example.com").status_code, 405)



class ObjectStorageSettingsTests(SimpleTestCase):
    """S3-compatible storage (AWS, R2, B2, ...) with two physically separate
    buckets; misconfiguration refuses to start."""

    def _settings(self, **changes):
        return object_storage_settings({**S3_ENV, **changes})

    def test_r2_style_endpoint_with_public_domain(self):
        config = self._settings()
        self.assertEqual(config["AWS_S3_ENDPOINT_URL"], "https://account.r2.example.com")
        self.assertEqual(config["AWS_S3_CUSTOM_DOMAIN"], "media.example.com")
        self.assertEqual(config["AWS_S3_REGION_NAME"], "auto")
        self.assertEqual(config["AWS_S3_SIGNATURE_VERSION"], "s3v4")
        self.assertIsNone(config["AWS_S3_ADDRESSING_STYLE"])
        self.assertEqual((config["AWS_STORAGE_BUCKET_NAME"], config["AWS_PRIVATE_STORAGE_BUCKET_NAME"]),
                         ("smerfume-public", "smerfume-private"))

    def test_aws_defaults_without_endpoint(self):
        config = self._settings(AWS_S3_ENDPOINT_URL="", AWS_S3_CUSTOM_DOMAIN="")
        self.assertIsNone(config["AWS_S3_ENDPOINT_URL"])
        self.assertIsNone(config["AWS_S3_CUSTOM_DOMAIN"])
        self.assertEqual(config["AWS_S3_REGION_NAME"], "ap-south-1")

    def test_explicit_region_and_path_addressing(self):
        config = self._settings(AWS_S3_REGION_NAME="us-west-004", AWS_S3_ADDRESSING_STYLE="path")
        self.assertEqual((config["AWS_S3_REGION_NAME"], config["AWS_S3_ADDRESSING_STYLE"]), ("us-west-004", "path"))

    def test_required_values(self):
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_STORAGE_BUCKET_NAME",
                     "AWS_PRIVATE_STORAGE_BUCKET_NAME"):
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, name):
                self._settings(**{name: " "})

    def test_public_and_private_buckets_must_differ(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "different"):
            self._settings(AWS_PRIVATE_STORAGE_BUCKET_NAME=" Smerfume-Public ")

    def test_custom_endpoint_needs_a_public_domain(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "AWS_S3_CUSTOM_DOMAIN"):
            self._settings(AWS_S3_CUSTOM_DOMAIN="")

    def test_custom_domain_is_a_bare_host(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "AWS_S3_CUSTOM_DOMAIN"):
            self._settings(AWS_S3_CUSTOM_DOMAIN="https://media.example.com/")

    def test_errors_do_not_echo_secrets(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="smerfume-public")
        self.assertNotIn("test-secret", str(ctx.exception))


@override_settings(
    AWS_ACCESS_KEY_ID="test-key-id", AWS_SECRET_ACCESS_KEY="test-secret",
    AWS_STORAGE_BUCKET_NAME="smerfume-public", AWS_PRIVATE_STORAGE_BUCKET_NAME="smerfume-private",
    AWS_S3_ENDPOINT_URL="https://account.r2.example.com", AWS_S3_CUSTOM_DOMAIN="media.example.com",
    AWS_S3_REGION_NAME="auto", AWS_S3_SIGNATURE_VERSION="s3v4",
)
class StorageBackendTests(SimpleTestCase):
    """The two backends never share a bucket or a public URL. No network:
    URLs are built (and private ones presigned) locally."""

    def test_public_media_storage(self):
        from apps.core.storage import PublicMediaStorage

        storage = PublicMediaStorage()
        self.assertEqual(storage.bucket_name, "smerfume-public")
        self.assertEqual(storage.custom_domain, "media.example.com")
        self.assertFalse(storage.querystring_auth)
        self.assertFalse(storage.file_overwrite)
        self.assertIsNone(storage.default_acl)
        self.assertEqual(storage.url("catalog/variants/a.webp"), "https://media.example.com/catalog/variants/a.webp")

    def test_private_media_storage(self):
        from apps.core.storage import PrivateMediaStorage

        storage = PrivateMediaStorage()
        self.assertEqual(storage.bucket_name, "smerfume-private")
        self.assertIsNone(storage.custom_domain)
        self.assertTrue(storage.querystring_auth)
        self.assertEqual(storage.querystring_expire, 300)
        self.assertFalse(storage.file_overwrite)
        self.assertIsNone(storage.default_acl)
        self.assertEqual(storage.location, "")
        url = storage.url("supplier-payments/x.pdf")
        self.assertNotIn("media.example.com", url)
        self.assertIn("smerfume-private", url)
        self.assertIn("X-Amz-Signature=", url)

    def test_options_cannot_point_private_at_the_public_domain(self):
        from apps.core.storage import PrivateMediaStorage

        with self.assertRaises(ImproperlyConfigured):
            PrivateMediaStorage(custom_domain="media.example.com")
        with self.assertRaises(ImproperlyConfigured):
            PrivateMediaStorage(bucket_name="smerfume-public")

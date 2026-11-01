import os
from datetime import timedelta
from .base import *

DEBUG = False

# ALLOWED_HOSTS is explicit; UAT_HOST (one host) is still accepted.
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS") or env_list("UAT_HOST")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must list at least one value in this environment.")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# ── Deployment (provider-neutral) ─────────────────────────────────────────
SECRET_KEY = require_env("SECRET_KEY")
DATABASES = {"default": database_config(default_conn_max_age=60)}
# Trust X-Forwarded-Proto only when the proxy in front always sets it;
# without it, SECURE_SSL_REDIRECT behind a TLS-terminating proxy loops.
SECURE_PROXY_SSL_HEADER = trusted_proxy_ssl_header()
LOGGING = logging_config(os.getenv("LOG_LEVEL", "INFO"))
# Static files are collected into the image and served by WhiteNoise.
STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# Shared throttle counters. Required: no LocMemCache fallback here.
CACHES = redis_cache(require_env("REDIS_URL"))
# Must match this environment's proxy topology (0 is a valid explicit value).
REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": parse_num_proxies(require_env("NUM_PROXIES"))}

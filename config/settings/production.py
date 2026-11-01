import os
from datetime import timedelta
from .base import *

DEBUG = False

ALLOWED_HOSTS = require_env_list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = require_env_list("CSRF_TRUSTED_ORIGINS")

# ── Deployment (provider-neutral) ─────────────────────────────────────────
SECRET_KEY = require_env("SECRET_KEY")
DATABASES = {"default": database_config(default_conn_max_age=60)}
# Trust X-Forwarded-Proto only when the proxy in front always sets it;
# without it, SECURE_SSL_REDIRECT behind a TLS-terminating proxy loops.
SECURE_PROXY_SSL_HEADER = trusted_proxy_ssl_header()

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# ── Security headers ───────────────────────────────────────────────────────────
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# Initial rollout (docs/DEPLOYMENT.md): short HSTS, no subdomains, no
# preload until every Smerfume subdomain is confirmed HTTPS-only.
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 300)
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Shared throttle counters. Required: no LocMemCache fallback here.
CACHES = redis_cache(require_env("REDIS_URL"))
# Must match this environment's proxy topology (0 is a valid explicit value).
REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": parse_num_proxies(require_env("NUM_PROXIES"))}

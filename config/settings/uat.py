import os
from datetime import timedelta
from .base import *

DEBUG = False

ALLOWED_HOSTS = [os.getenv("UAT_HOST", "uat.smerfume.com")]

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
REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": int(require_env("NUM_PROXIES"))}

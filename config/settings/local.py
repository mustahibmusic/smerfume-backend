from .base import *
import os
from datetime import timedelta

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

ADMIN_ENVIRONMENT = os.getenv("ADMIN_ENVIRONMENT", "LOCAL")

CORS_ALLOW_ALL_ORIGINS = True

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# Relax throttle in local dev
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        "anon": "1000/min",
        "user": "3000/min",
        "otp_request": "100/hour",
        "otp_verify": "1000/minute",
    },
}

# Redis is optional locally; without REDIS_URL Django uses LocMemCache.
REDIS_URL = os.getenv("REDIS_URL", "").strip()
if REDIS_URL:
    CACHES = redis_cache(REDIS_URL)

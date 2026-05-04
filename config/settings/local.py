from .base import *
from datetime import timedelta
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]


SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'AUTH_HEADER_TYPES': ('Bearer',),
}
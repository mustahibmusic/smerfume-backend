"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os
from dotenv import load_dotenv

from django.core.asgi import get_asgi_application

_env = os.getenv("DJANGO_ENV", "local")
load_dotenv(f".env.{_env}")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", f"config.settings.{_env}")

application = get_asgi_application()

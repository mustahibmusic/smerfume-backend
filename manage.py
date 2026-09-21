import os
from dotenv import load_dotenv

from config.env_resolution import resolve_django_env

# Load the environment-specific .env file.
# Change DJANGO_ENV at the OS/server level to switch environments.
# Supported values: local | staging | uat | production
_env = resolve_django_env(fail_closed=False)
load_dotenv(f".env.{_env}")

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    f"config.settings.{_env}"
)

from django.core.management import execute_from_command_line

execute_from_command_line()

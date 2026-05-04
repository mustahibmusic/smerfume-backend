import os
from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    f"config.settings.{os.getenv('DJANGO_ENV', 'local')}"
)

from django.core.management import execute_from_command_line

execute_from_command_line()

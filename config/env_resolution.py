"""
Resolves which Django settings module to load, and fails loudly instead
of silently defaulting to insecure local/dev settings when misconfigured.

Two entrypoint classes:
    fail_closed=True  — wsgi.py, asgi.py. These serve real traffic; an
        unset or unrecognized DJANGO_ENV must crash the process at boot
        rather than silently loading config.settings.local (DEBUG=True,
        CORS_ALLOW_ALL_ORIGINS=True).
    fail_closed=False — manage.py. Local developer convenience; an unset
        DJANGO_ENV defaults to "local" so `python manage.py runserver` /
        `test` / `makemigrations` keep working with zero configuration.
"""

import os

ALLOWED_ENVS = {"local", "staging", "uat", "production"}


class DjangoEnvError(RuntimeError):
    """DJANGO_ENV is missing or invalid for a fail-closed entrypoint."""


def resolve_django_env(*, fail_closed: bool) -> str:
    raw = os.getenv("DJANGO_ENV")

    if not fail_closed:
        return raw or "local"

    if not raw:
        raise DjangoEnvError(
            "DJANGO_ENV must be set explicitly in this environment "
            f"(expected one of {sorted(ALLOWED_ENVS)}). Refusing to "
            "silently fall back to 'local' settings, which are insecure "
            "for serving real traffic (DEBUG=True, CORS wide open)."
        )
    if raw not in ALLOWED_ENVS:
        raise DjangoEnvError(
            f"DJANGO_ENV={raw!r} is not a recognized environment "
            f"(expected one of {sorted(ALLOWED_ENVS)})."
        )
    return raw

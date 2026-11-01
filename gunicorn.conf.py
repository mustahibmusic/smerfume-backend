"""Gunicorn settings, from the environment only (no provider specifics).

PORT             listen port (default 8000)
WEB_CONCURRENCY  worker processes (default 1: fits a 512 MB container
                 with Pillow image processing; raise after measuring RAM)
GUNICORN_TIMEOUT seconds before a busy worker is restarted (default 120,
                 for gallery image processing)
"""

import os


def _positive_int(name, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(raw)


bind = f"0.0.0.0:{_positive_int('PORT', 8000)}"
workers = _positive_int("WEB_CONCURRENCY", 1)
timeout = _positive_int("GUNICORN_TIMEOUT", 120)
accesslog = "-"  # stdout
errorlog = "-"  # stderr
# Worker heartbeat in memory, not on a possibly slow container disk.
worker_tmp_dir = "/dev/shm"

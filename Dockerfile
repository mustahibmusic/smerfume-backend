# Smerfume backend: one provider-neutral production image.
# Runs unchanged on Railway, Render, Fly.io or any container host / VPS.
# Configuration comes only from environment variables (see .env.example);
# DJANGO_ENV must be set at runtime (production, staging or uat).
# Release step (run once per deploy, not here): docs/DEPLOYMENT.md.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Static files are collected into the image and served by WhiteNoise.
# Build-only placeholder values: nothing connects to a database or Redis.
RUN DJANGO_ENV=production \
    SECRET_KEY=collectstatic-build-only \
    ALLOWED_HOSTS=localhost \
    CSRF_TRUSTED_ORIGINS=https://localhost \
    REDIS_URL=redis://localhost:6379/0 \
    NUM_PROXIES=0 \
    python manage.py collectstatic --noinput

# Non-root runtime user. media/ and private_media/ exist only for hosts
# that mount a volume instead of using object storage (USE_S3=True).
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /app/media /app/private_media \
    && chown app /app/media /app/private_media
USER app

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"]

# Deployment

How to run the Smerfume backend on any container host (Railway, Render,
Fly.io, a VPS with Docker, ...). The application has no provider-specific
code: one Docker image, configured only by environment variables. No
provider has been selected yet.

Not in scope (deferred until needed): read replicas, database routers,
PgBouncer, Celery/workers, multiple web instances, multi-region, HA
databases, paid monitoring.

## 1. What runs where

| Piece | Where |
|---|---|
| Django + DRF + admin | One container from `Dockerfile`, Gunicorn |
| Static files (admin, Unfold, Cropper) | Inside the image, served by WhiteNoise |
| PostgreSQL | Any managed or self-run PostgreSQL (`DATABASE_URL`) |
| Redis (DRF throttle counters) | Any Redis (`REDIS_URL`, `rediss://` for TLS) |
| Product images | Public S3-compatible bucket |
| Supplier payment proof | Separate private S3-compatible bucket |

Local development is unchanged: `python manage.py runserver` with
`.env.local` (`DB_*` variables, optional `REDIS_URL`, filesystem media).

## 2. Environment

`.env.example` lists every variable with safe sample values. Set real
values in the provider's environment settings; never commit them.

- `DJANGO_ENV` must be `production`, `staging` or `uat`. The WSGI entry point
  refuses to start without it.
- Required in deployed environments: `SECRET_KEY`, `ALLOWED_HOSTS`,
  `REDIS_URL`, `NUM_PROXIES`, and in production `CSRF_TRUSTED_ORIGINS`.
- Database: `DATABASE_URL` (parsed by dj-database-url; `?sslmode=require`
  is honoured). `DB_SSLMODE` overrides the URL's SSL mode. Deployed
  environments keep connections for `CONN_MAX_AGE` seconds (default 60)
  with health checks. Local development uses `DB_*` first.

## 3. Build

```
docker build -t smerfume-backend .
```

The build installs dependencies and runs `collectstatic` with build-only
placeholder values (nothing connects to the database or Redis). The image
runs as a non-root user. `.dockerignore` keeps `.env*`, `.git`, `.claude`,
local media, caches and virtualenvs out of the image.

## 4. Release (once per deploy, before the new web process starts)

```
python manage.py migrate --noinput
python manage.py check --deploy
```

`migrate` runs Django's system checks before it migrates, including on
the first deploy against an empty database, so no system check may query
the database. Unfold's admin check queries `auth_permission` for action
permissions written as `"app.codename"`; Smerfume admin actions therefore
use `has_<name>_permission()` methods instead
(`apps.core.tests.SystemChecksNeedNoDatabaseTests` enforces this).

Run these as a separate one-off process using the same image and
environment. Never run migrations in the Docker `CMD`, at app startup or
on every worker boot.

Where each provider runs a release command (configuration, not code):

| Provider | Setting |
|---|---|
| Render | Service "Pre-Deploy Command" |
| Railway | Service "Pre-Deploy Command" |
| Fly.io | `[deploy] release_command` in `fly.toml` |
| VPS / Docker Compose | `docker compose run --rm web sh -c "python manage.py migrate --noinput && python manage.py check --deploy"` before `docker compose up -d` |

Rules:

- Migrations must work while the previous version is still serving (add
  first, remove in a later release).
- After migrations that add permissions, run `python manage.py
  setup_staff_groups --dry-run`, then without `--dry-run`.
- `check --deploy` needs the database to be reachable (Unfold's admin check
  queries it), so run it in the release step, not during the image build.
- `check --deploy` currently reports W005 (HSTS includeSubDomains) and W021
  (HSTS preload) as warnings. That is intentional during the initial
  rollout (section 7). It fails only on errors.

## 5. Start

The image `CMD` starts Gunicorn only (`gunicorn.conf.py`):

- `PORT` (default 8000), `WEB_CONCURRENCY` (default 1), `GUNICORN_TIMEOUT`
  (default 120 seconds for gallery image processing).
- One worker fits a 512 MB container with Pillow processing. Measure memory
  before raising `WEB_CONCURRENCY` to 2 or more.
- Access log to stdout, errors to stderr.

## 6. Health checks

| Path | Use | What it does |
|---|---|---|
| `/healthz/` | Provider health / liveness check | Fixed `ok`, no database or Redis, answers before host and HTTPS checks so internal probes work. Exact path, GET/HEAD only. |
| `/readyz/` | Post-deploy verification, uptime monitor | `SELECT 1` on the database: `200 ok` or `503 unavailable`. Normal host and HTTPS handling (send a valid Host and, behind a proxy, `X-Forwarded-Proto: https`). Does not check Redis. |

Point the provider's health check at `/healthz/`. Do not use `/readyz/` as
a liveness check: a short database outage would then restart healthy
containers in a loop.

## 7. HTTPS, proxies and HSTS

- The provider's proxy ends TLS. Set `TRUST_PROXY_SSL_HEADER=True` only when
  that proxy always sets or overwrites `X-Forwarded-Proto` (Render, Railway
  and Fly.io do; on a VPS configure nginx/Caddy to). Without it,
  `SECURE_SSL_REDIRECT` redirects forever behind the proxy.
- `NUM_PROXIES` must equal the number of proxies in front, for client IPs
  in throttling.
- `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (full `https://` origins) are
  explicit lists.

HSTS rollout:

1. Initial: `SECURE_HSTS_SECONDS=300`, no `includeSubDomains`, no `preload`.
2. After HTTPS has been stable for some days: raise to `86400`, then
   `2592000` (30 days), then `31536000` (1 year).
3. Only when every `*.smerfume.com` subdomain (including UAT and staging)
   is confirmed HTTPS-only: enable `includeSubDomains` (code change), and
   only then consider `preload`. Preload is effectively permanent.

## 8. Object storage

`USE_S3=True` stores uploads in S3-compatible storage (AWS S3, Cloudflare
R2, Backblaze B2, ...). Two physically separate buckets:

- Public (`AWS_STORAGE_BUCKET_NAME`): product and gallery images, readable
  through `AWS_S3_CUSTOM_DOMAIN` (bare host, e.g. `media.smerfume.com`).
- Private (`AWS_PRIVATE_STORAGE_BUCKET_NAME`): supplier payment proof and
  future protected documents. Block all public access on this bucket.
  Files are only served through the permission-checked admin download.

The app refuses to start if the private bucket is missing or is the same
as the public bucket, or if a custom `AWS_S3_ENDPOINT_URL` has no
`AWS_S3_CUSTOM_DOMAIN` (a provider's API endpoint is not a public URL).
Leave `AWS_S3_ENDPOINT_URL` blank for AWS S3. Use `AWS_S3_REGION_NAME=auto`
for R2.

Without `USE_S3`, uploads go to the container filesystem, which most
container hosts erase on every deploy. Only use that with a persistent
volume mounted at `/app/media` and `/app/private_media`.

## 9. Uploads

- Gallery: up to 15 MB per image, up to 20 images per variant
  (`apps/catalog/image_processing.py`). A request uploading many large
  images at once can be large; the app and Gunicorn do not cap it, but a
  provider or reverse proxy may. On a VPS set the proxy's body limit
  (nginx `client_max_body_size`) to cover it. Upload in smaller batches
  if a provider limit is hit.
- Payment proof: up to 5 files, 5 MB each.
- Files above Django's in-memory limit stream to `/tmp` in the container.

## 10. Logs

Deployed environments log to stdout (INFO and below) and stderr (WARNING
and above, including unhandled request errors with tracebacks). The
provider collects both. `LOG_LEVEL` sets the level. Tracebacks never
appear in HTTP responses.

## 11. API documentation

`/api/schema/`, `/api/docs/` and `/api/redoc/` are open in local
development only. In UAT, staging and production they require a logged-in
active staff user (log in through `/admin/` first).

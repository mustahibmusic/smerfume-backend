"""S3-compatible object storage (AWS S3, Cloudflare R2, Backblaze B2, ...).

Two physically separate buckets with an explicit code boundary:

- PublicMediaStorage: product and gallery images. Public bucket, optional
  custom domain or CDN, plain (unsigned) URLs.
- PrivateMediaStorage: supplier payment proof and future protected
  documents. Private bucket, no custom domain, signed short-lived URLs,
  served only through permission-checked Django views.

Endpoint, credentials, region and addressing style come from the shared
AWS_* settings that django-storages reads; see config.settings.base
object_storage_settings(). No provider SDK beyond boto3.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from storages.backends.s3 import S3Storage


class PublicMediaStorage(S3Storage):
    def get_default_settings(self):
        defaults = super().get_default_settings()
        defaults.update({
            "bucket_name": settings.AWS_STORAGE_BUCKET_NAME,
            "custom_domain": getattr(settings, "AWS_S3_CUSTOM_DOMAIN", None) or None,
            "querystring_auth": False,
            "file_overwrite": False,
            "default_acl": None,
        })
        return defaults


class PrivateMediaStorage(S3Storage):
    def get_default_settings(self):
        defaults = super().get_default_settings()
        defaults.update({
            "bucket_name": settings.AWS_PRIVATE_STORAGE_BUCKET_NAME,
            "custom_domain": None,
            "querystring_auth": True,
            "querystring_expire": 300,
            "file_overwrite": False,
            "default_acl": None,
        })
        return defaults

    def __init__(self, **options):
        super().__init__(**options)
        # Never publish private files: no public domain, never the public bucket.
        if self.custom_domain or not self.querystring_auth:
            raise ImproperlyConfigured("Private storage must not use a public domain or unsigned URLs.")
        public = (getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "").strip().lower()
        if not self.bucket_name or self.bucket_name.strip().lower() == public:
            raise ImproperlyConfigured("Private storage needs its own bucket, different from the public one.")

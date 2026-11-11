# apps/core/models.py

import uuid
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BaseModel(TimeStampedModel):
    id = models.BigAutoField(primary_key=True)

    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True


class OpsAccess(models.Model):
    """Permission holder only: no table, no rows.

    Carries ``core.access_ops``, the explicit gate to the Smerfume
    Operations backend (/ops/). Separate from Django Admin access
    (``is_staff``): neither implies the other.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [("access_ops", "Can access Smerfume Operations")]

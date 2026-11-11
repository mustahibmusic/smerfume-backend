"""Access boundary for /ops/.

``core.access_ops`` is the only gate to /ops/. It is separate from Django
Admin access (``is_staff``): neither implies the other.
"""

from django.contrib import admin

OPS_ACCESS_PERMISSION = "core.access_ops"


def has_ops_access(user):
    return bool(
        user is not None
        and user.is_authenticated
        and user.is_active
        and user.has_perm(OPS_ACCESS_PERMISSION)
    )


def has_admin_access(request):
    """True when Django Admin itself would let this user in.

    Delegates to the admin site's own rule so /ops/ never shows an admin
    link that admin would refuse.
    """
    return admin.site.has_permission(request)

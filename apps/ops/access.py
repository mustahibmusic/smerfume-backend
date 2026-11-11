"""Access boundary for /ops/.

``core.access_ops`` is the only gate to /ops/. It is separate from Django
Admin access (``is_staff``): neither implies the other.
"""

OPS_ACCESS_PERMISSION = "core.access_ops"


def has_ops_access(user):
    return bool(
        user is not None
        and user.is_authenticated
        and user.is_active
        and user.has_perm(OPS_ACCESS_PERMISSION)
    )

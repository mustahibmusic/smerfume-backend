"""Sidebar registry for /ops/.

Each item names the model permission a user needs to see it. Hiding an
item is display only: every view enforces its own permission too.
"""

from dataclasses import dataclass

from django.urls import reverse

from apps.ops.access import has_ops_access


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    url_name: str
    permission: str
    icon: str


NAV_ITEMS = (
    NavItem(
        key="orders",
        label="Orders",
        url_name="ops:order_list",
        permission="orders.view_order",
        icon="orders",
    ),
)


def nav_for(user, active=""):
    """Visible sidebar items for ``user``; empty without /ops/ access."""
    if not has_ops_access(user):
        return []
    return [
        {
            "key": item.key,
            "label": item.label,
            "url": reverse(item.url_name),
            "icon": item.icon,
            "active": item.key == active,
        }
        for item in NAV_ITEMS
        if user.has_perm(item.permission)
    ]

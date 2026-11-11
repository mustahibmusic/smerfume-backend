"""Sidebar and home quick-access registry for the Smerfume Panel (/ops/).

Only implemented pages are registered. Each item names the model
permission a user needs to see it. Showing or hiding an item is display
only: every view enforces its own permission too.
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
    description: str = ""


@dataclass(frozen=True)
class NavGroup:
    key: str
    label: str
    items: tuple = ()


HOME = NavItem(key="home", label="Home", url_name="ops:home", permission="", icon="home")

# Business groups in sidebar order. A group with no visible items is not
# shown, so groups without implemented pages stay hidden.
NAV_GROUPS = (
    NavGroup("sales", "Sales", (
        NavItem(
            key="orders",
            label="Orders",
            url_name="ops:order_list",
            permission="orders.view_order",
            icon="orders",
            description="Find orders and view items, payments and returns.",
        ),
    )),
    NavGroup("catalog", "Catalog"),
    NavGroup("inventory", "Inventory"),
    NavGroup("purchases", "Purchases"),
    NavGroup("customers", "Customers"),
)


def _entry(item, active):
    return {
        "key": item.key,
        "label": item.label,
        "url": reverse(item.url_name),
        "icon": item.icon,
        "description": item.description,
        "active": item.key == active,
    }


def nav_for(user, active=""):
    """Sidebar for ``user``, or None without /ops/ access.

    ``{"home": entry, "groups": [{"key", "label", "open", "items"}]}``.
    The group holding the active page is open.
    """
    if not has_ops_access(user):
        return None
    groups = []
    for group in NAV_GROUPS:
        items = [_entry(item, active) for item in group.items if user.has_perm(item.permission)]
        if items:
            groups.append({
                "key": group.key,
                "label": group.label,
                "items": items,
                "open": any(item["active"] for item in items),
            })
    return {"home": _entry(HOME, active), "groups": groups}


def quick_links(user):
    """Visible pages for the home quick-access cards, in sidebar order."""
    nav = nav_for(user)
    if nav is None:
        return []
    return [
        {**item, "group": group["label"]}
        for group in nav["groups"]
        for item in group["items"]
    ]

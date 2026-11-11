"""Sidebar and home quick-access registry for the Smerfume Panel (/ops/).

The sidebar lists the planned panel pages. An item with a ``url_name`` is
implemented and links to its page; an item without one is shown disabled
as "Coming soon" and leads nowhere. Every item names the model permission
a user needs to see it at all (the same permission Django Admin's sidebar
uses for that area), so users never see areas they may not access.
Showing an item is display only: every view enforces its own permission.
"""

from dataclasses import dataclass

from django.urls import reverse

from apps.ops.access import has_ops_access


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    permission: str
    url_name: str = ""  # empty: not implemented yet ("Coming soon")
    icon: str = "dot"
    description: str = ""


@dataclass(frozen=True)
class NavGroup:
    key: str
    label: str
    items: tuple = ()


HOME = NavItem(key="home", label="Home", permission="", url_name="ops:home", icon="home")

NAV_GROUPS = (
    NavGroup("sales", "Sales", (
        NavItem(
            "orders", "Orders", "orders.view_order", "ops:order_list", icon="orders",
            description="Find orders and view items, payments and returns.",
        ),
        NavItem(
            "in_store_sale", "New In-store Sale", "orders.add_order", "ops:in_store_sale", icon="sale",
            description="Record a walk-in counter sale.",
        ),
        NavItem(
            "returns", "Returns", "orders.view_return", "ops:return_list", icon="returns",
            description="Approve, receive and inspect customer returns.",
        ),
        NavItem(
            "refunds", "Refunds", "orders.view_refund", "ops:refund_list", icon="refunds",
            description="Choose method, process and complete refunds.",
        ),
    )),
    NavGroup("catalog", "Catalog", (
        NavItem("products", "Products", "catalog.view_product"),
        NavItem("editions", "Editions", "catalog.view_productedition"),
        NavItem("variants", "Variants", "catalog.view_productvariant"),
        NavItem("brands", "Brands", "catalog.view_brand"),
        NavItem("perfume_notes", "Perfume Notes", "catalog.view_perfumenote"),
    )),
    NavGroup("inventory", "Inventory", (
        NavItem(
            "stock", "Stock", "inventory.view_inventorystock", "ops:stock_list", icon="stock",
            description="Stock by item, type and warehouse; adjust and transfer.",
        ),
        NavItem(
            "partial_bottles", "Partial Bottles", "inventory.view_partialbottlelot", "ops:partial_lot_list",
            icon="bottle", description="Open bottles used for decants.",
        ),
        NavItem(
            "stock_movements", "Stock Movements", "inventory.view_stockmovement", "ops:movement_list",
            icon="movements", description="Every stock change, newest first.",
        ),
        NavItem(
            "reservations", "Reservations", "inventory.view_stockreservation", "ops:reservation_list",
            icon="reservations", description="Stock held for orders.",
        ),
        NavItem(
            "warehouses", "Warehouses", "inventory.view_warehouse", "ops:warehouse_list", icon="warehouse",
            description="Stock locations (read-only).",
        ),
    )),
    NavGroup("purchases", "Purchases", (
        NavItem(
            "vendors", "Vendors", "inventory.view_supplier", "ops:vendor_list", icon="vendor",
            description="Supplier details and their purchase orders.",
        ),
        NavItem(
            "purchase_orders", "Purchase Orders", "purchases.view_purchaseorder", "ops:po_list", icon="po",
            description="Issue, confirm bookings, close and pay purchase orders.",
        ),
        NavItem(
            "goods_receipts", "Goods Receipts", "purchases.view_goodsreceipt", "ops:grn_list", icon="receipt",
            description="Post, cancel and reverse goods receipts.",
        ),
        NavItem(
            "supplier_payments", "Supplier Payments", "purchases.view_purchaseorderpayment", "ops:payment_list",
            icon="refunds", description="Payment ledger with drafts and proof.",
        ),
        NavItem(
            "paid_through", "Paid Through Accounts", "purchases.view_paymentaccount", "ops:account_list",
            icon="wallet", description="Accounts payments are made from (read-only).",
        ),
        NavItem(
            "receipt_discrepancies", "Receipt Discrepancies", "purchases.view_receiptdiscrepancy",
            "ops:discrepancy_list", icon="alert", description="Delivery problems to resolve.",
        ),
        NavItem(
            "booked_quantity_history", "Booked Quantity History", "purchases.view_bookedquantitychange",
            "ops:booked_list", icon="history", description="Vendor-confirmed quantity changes.",
        ),
    )),
    NavGroup("customers", "Customers", (
        NavItem("customers", "Customers", "accounts.view_customerprofile"),
        NavItem("users", "Users", "accounts.view_user"),
    )),
)


def _entry(item, active):
    implemented = bool(item.url_name)
    return {
        "key": item.key,
        "label": item.label,
        "url": reverse(item.url_name) if implemented else None,
        "icon": item.icon,
        "description": item.description,
        "implemented": implemented,
        "active": implemented and item.key == active,
    }


def nav_for(user, active=""):
    """Sidebar for ``user``, or None without /ops/ access.

    ``{"home": entry, "groups": [{"key", "label", "open", "items"}]}``.
    Items the user lacks permission for are left out; a group with no
    items left is not shown. The group holding the active page is open.
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
    """Implemented, permitted pages for the home quick-access cards."""
    nav = nav_for(user)
    if nav is None:
        return []
    return [
        {**item, "group": group["label"]}
        for group in nav["groups"]
        for item in group["items"]
        if item["implemented"]
    ]

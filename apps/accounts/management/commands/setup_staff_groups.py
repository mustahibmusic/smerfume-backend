"""Create or update the four staff groups (Ops, Warehouse, Finance,
Manager) with an explicit, least-privilege permission set.

Safe to rerun: each group's permissions are SET to the list below, so
permissions added to a group by hand are removed. Users are never touched;
assigning staff to groups stays a manual step for a superuser. No group
ever gets auth/admin permissions, superuser status, or add/change/delete
on stock ledgers.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


def _view(app_label, *models):
    return tuple(f"{app_label}.view_{model}" for model in models)


# Deleting these rows is only possible on drafts (the admin inlines refuse
# it otherwise) or is ordinary catalog content editing; needed so staff can
# remove a mistaken line before posting/issuing.
DRAFT_INLINE_DELETES = frozenset({
    "purchases.delete_purchaseorderline",
    "purchases.delete_goodsreceiptline",
    "purchases.delete_receiptdiscrepancy",
    "catalog.delete_productvariantimage",
    "catalog.delete_editionnote",
})

CATALOG_MODELS = (
    "brand", "category", "perfumenote", "product", "productedition",
    "productvariant", "productvariantimage", "editionnote",
)

OPS = (
    *_view("orders", "order", "orderitem", "shippingaddress", "return", "returnitem", "refund"),
    *_view("accounts", "user", "customerprofile"),
    *_view("cart", "cart", "cartitem"),
    *_view("inventory", "inventorystock", "stockreservation"),
    *_view("catalog", *CATALOG_MODELS),
    "orders.add_order",
    "orders.verify_cod_order",
    "orders.cancel_order",
    "orders.process_return",
    "accounts.change_user",
    "accounts.change_customerprofile",
)

WAREHOUSE = (
    *_view("orders", "order", "orderitem", "shippingaddress", "return", "returnitem"),
    *_view(
        "inventory", "inventorystock", "partialbottlelot", "stockmovement", "stocktransaction",
        "stockreservation", "stockreservationallocation", "supplier", "warehouse",
    ),
    *_view(
        "purchases", "purchaseorder", "purchaseorderline", "goodsreceipt", "goodsreceiptline",
        "receiptdiscrepancy", "bookedquantitychange",
    ),
    *_view("catalog", *CATALOG_MODELS),
    "orders.pack_order",
    "orders.ship_order",
    "orders.deliver_order",
    "orders.receive_return",
    "orders.change_return",
    "orders.change_returnitem",
    "inventory.change_inventorystock",
    "inventory.transfer_stock_type",
    "inventory.change_partialbottlelot",
    "purchases.add_goodsreceipt",
    "purchases.change_goodsreceipt",
    "purchases.post_goodsreceipt",
    "purchases.cancel_goodsreceipt",
    "purchases.add_goodsreceiptline",
    "purchases.change_goodsreceiptline",
    "purchases.delete_goodsreceiptline",
    "purchases.add_receiptdiscrepancy",
    "purchases.change_receiptdiscrepancy",
    "purchases.delete_receiptdiscrepancy",
    "purchases.resolve_receiptdiscrepancy",
)

FINANCE = (
    *_view("orders", "order", "orderitem", "return", "returnitem", "refund", "refundadjustment"),
    *_view("accounts", "customerprofile"),
    *_view("inventory", "inventorystock", "stockmovement", "stocktransaction", "supplier"),
    *_view(
        "purchases", "purchaseorder", "purchaseorderline", "goodsreceipt", "goodsreceiptline",
        "receiptdiscrepancy",
    ),
    "orders.change_refund",
    "orders.process_refund",
    "orders.complete_refund",
    "orders.add_refundadjustment",
)

MANAGER_EXTRA = (
    *_view("orders", "refundadjustment"),
    *_view(
        "inventory", "partialbottlelot", "stockmovement", "stocktransaction", "stockreservation",
        "stockreservationallocation", "supplier", "warehouse", "decantsource",
    ),
    *_view("accounts", "user"),
    "purchases.add_purchaseorder",
    "purchases.change_purchaseorder",
    "purchases.issue_purchaseorder",
    "purchases.cancel_purchaseorder",
    "purchases.close_purchaseorder",
    "purchases.add_purchaseorderline",
    "purchases.change_purchaseorderline",
    "purchases.delete_purchaseorderline",
    "purchases.confirm_booked_quantity",
    "purchases.reverse_goodsreceipt",
    "inventory.reallocate_incoming_allocation",
    "inventory.add_supplier",
    "inventory.change_supplier",
    "inventory.add_warehouse",
    "inventory.change_warehouse",
    "inventory.add_decantsource",
    "inventory.change_decantsource",
    *(f"catalog.{verb}_{model}" for model in CATALOG_MODELS for verb in ("add", "change")),
    "catalog.delete_productvariantimage",
    "catalog.delete_editionnote",
)

GROUP_PERMISSIONS = {
    "Ops": OPS,
    "Warehouse": WAREHOUSE,
    "Finance": FINANCE,
    "Manager": OPS + WAREHOUSE + FINANCE + MANAGER_EXTRA,
}

FORBIDDEN_APPS = {"auth", "admin", "contenttypes", "sessions"}


def group_permissions():
    """{group name: sorted unique 'app_label.codename' codes}."""
    return {name: sorted(set(codes)) for name, codes in GROUP_PERMISSIONS.items()}


class Command(BaseCommand):
    help = "Create or update the Ops, Warehouse, Finance and Manager staff groups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Show the changes without writing them.",
        )

    def handle(self, *args, dry_run=False, **options):
        wanted = group_permissions()
        all_codes = sorted(set().union(*wanted.values()))

        unsafe = [
            code for code in all_codes
            if code.split(".")[0] in FORBIDDEN_APPS
            or (code.split(".")[1].startswith("delete_") and code not in DRAFT_INLINE_DELETES)
        ]
        if unsafe:
            raise CommandError(f"Refusing unsafe permissions: {', '.join(unsafe)}")

        found = {
            f"{p.content_type.app_label}.{p.codename}": p
            for p in Permission.objects.select_related("content_type")
        }
        missing = [code for code in all_codes if code not in found]
        if missing:
            raise CommandError(
                f"Missing permissions (run migrations first?): {', '.join(missing)}"
            )

        with transaction.atomic():
            for name, codes in wanted.items():
                group = Group.objects.filter(name=name).first()
                current = set()
                if group is not None:
                    current = {
                        f"{p.content_type.app_label}.{p.codename}"
                        for p in group.permissions.select_related("content_type")
                    }
                added = sorted(set(codes) - current)
                removed = sorted(current - set(codes))
                self.stdout.write(
                    f"{name}: {len(codes)} permissions (+{len(added)} / -{len(removed)})"
                    + (" [new group]" if group is None else "")
                )
                for code in added:
                    self.stdout.write(f"  + {code}")
                for code in removed:
                    self.stdout.write(f"  - {code}")
                if dry_run:
                    continue
                if group is None:
                    group = Group.objects.create(name=name)
                group.permissions.set([found[code] for code in codes])

        if dry_run:
            self.stdout.write("Dry run: nothing was changed.")

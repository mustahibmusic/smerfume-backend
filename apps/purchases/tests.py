"""
Tests for apps.purchases (P1B — purchase orders).

    PurchaseOrderModelTests       — numbering, validation, DB constraints
    PurchaseOrderCalculationTests — gross / discounted / taxable / unit cost
    PurchaseOrderWorkflowTests    — issue / cancel services and transitions
    PurchaseOrderInventoryTests   — no P1B operation touches inventory
    PurchaseOrderAdminTests       — editability, actions, permissions, nav
"""

from decimal import Decimal

from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.inventory.models import InventoryStock, StockMovement, StockTransaction, Supplier, Warehouse
from apps.purchases import services
from apps.purchases.admin import PurchaseOrderAdmin, PurchaseOrderLineInline
from apps.purchases.models import PurchaseOrder, PurchaseOrderLine

User = get_user_model()


def _variant(sku, is_decant=False):
    brand = Brand.objects.get_or_create(name="POTestBrand", slug="potestbrand")[0]
    category = Category.objects.get_or_create(name="POTestCat", slug="potestcat")[0]
    product = Product.objects.get_or_create(
        name="POTestProduct", slug="potestproduct",
        defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="potestproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition, size_ml=10 if is_decant else 100, is_decant=is_decant,
        selling_price="4500.00", mrp="5000.00", sku=sku,
    )


class _POBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="postaff", email="postaff@example.com", password="testpass123",
        )
        self.supplier = Supplier.objects.create(name="PO Vendor")
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _variant("SKU-PO-100ML")

    def _po(self, **kwargs):
        kwargs.setdefault("supplier", self.supplier)
        kwargs.setdefault("warehouse", self.warehouse)
        return PurchaseOrder.objects.create(created_by=self.user, **kwargs)

    def _line(self, po, **kwargs):
        kwargs.setdefault("variant", self.variant)
        kwargs.setdefault("quantity_ordered", 10)
        kwargs.setdefault("unit_price", Decimal("3100.00"))
        return PurchaseOrderLine.objects.create(purchase_order=po, **kwargs)


class PurchaseOrderModelTests(_POBase):
    def test_po_number_generated_and_stable(self):
        po = self._po()
        self.assertEqual(po.po_number, f"PO-{po.pk:05d}")
        po.notes = "changed"
        po.save()
        po.refresh_from_db()
        self.assertEqual(po.po_number, f"PO-{po.pk:05d}")
        self.assertNotEqual(po.po_number, self._po().po_number)

    def test_new_po_is_draft_and_full_clean_works_unsaved(self):
        po = PurchaseOrder(supplier=self.supplier, warehouse=self.warehouse)
        po.full_clean()
        self.assertEqual(po.status, PurchaseOrder.STATUS_DRAFT)
        self.assertEqual(str(po), "New purchase order")

    def test_expected_date_before_order_date_invalid(self):
        po = PurchaseOrder(
            supplier=self.supplier, warehouse=self.warehouse,
            order_date="2026-09-20", expected_date="2026-09-10",
        )
        with self.assertRaises(ValidationError):
            po.full_clean()

    def test_same_variant_allowed_on_multiple_lines(self):
        po = self._po()
        self._line(po, unit_price=Decimal("3000.00"))
        self._line(po, unit_price=Decimal("2900.00"))
        self.assertEqual(po.lines.count(), 2)

    def _assert_db_rejects(self, **kwargs):
        po = self._po()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._line(po, **kwargs)

    def test_db_rejects_zero_quantity(self):
        self._assert_db_rejects(quantity_ordered=0)

    def test_db_rejects_negative_price(self):
        self._assert_db_rejects(unit_price=Decimal("-1"))

    def test_db_rejects_negative_discount(self):
        self._assert_db_rejects(line_discount_amount=Decimal("-1"))

    def test_db_rejects_discount_above_gross(self):
        self._assert_db_rejects(
            quantity_ordered=2, unit_price=Decimal("100"), line_discount_amount=Decimal("200.01")
        )

    def test_db_rejects_tax_rate_out_of_range(self):
        self._assert_db_rejects(tax_rate=Decimal("100.01"))
        self._assert_db_rejects(tax_rate=Decimal("-0.01"))

    def test_line_validation(self):
        po = self._po()
        bad_lines = [
            {"line_discount_amount": Decimal("50000")},
            {"tax_rate": Decimal("101")},
            {"hsn_code": "12"},
            {"quantity_ordered": 0},
            {"variant": _variant("SKU-PO-DECANT", is_decant=True)},
        ]
        for extra in bad_lines:
            fields = {"variant": self.variant, "quantity_ordered": 10, "unit_price": Decimal("100")}
            fields.update(extra)
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                PurchaseOrderLine(purchase_order=po, **fields).full_clean()

    def test_historical_line_stays_valid_after_variant_deactivated(self):
        po = self._po()
        line = self._line(po)
        self.variant.is_active = False
        self.variant.save()
        line.refresh_from_db()
        line.full_clean()


class PurchaseOrderCalculationTests(_POBase):
    def test_tax_exclusive_line(self):
        po = self._po(amounts_include_tax=False)
        line = self._line(
            po, quantity_ordered=3, unit_price=Decimal("100.00"),
            line_discount_amount=Decimal("10.00"), tax_rate=Decimal("18"),
        )
        self.assertEqual(line.gross_line_amount, Decimal("300.00"))
        self.assertEqual(line.discounted_line_amount, Decimal("290.00"))
        self.assertEqual(line.taxable_value, Decimal("290.00"))
        self.assertEqual(line.effective_unit_cost_ex_tax, Decimal("96.6667"))

    def test_tax_inclusive_line(self):
        po = self._po(amounts_include_tax=True)
        line = self._line(
            po, quantity_ordered=2, unit_price=Decimal("1180.00"),
            line_discount_amount=Decimal("0"), tax_rate=Decimal("18"),
        )
        self.assertEqual(line.discounted_line_amount, Decimal("2360.00"))
        self.assertEqual(line.taxable_value, Decimal("2000.00"))
        self.assertEqual(line.effective_unit_cost_ex_tax, Decimal("1000.0000"))

    def test_tax_inclusive_without_rate_has_no_taxable_value(self):
        po = self._po(amounts_include_tax=True)
        line = self._line(po)
        self.assertIsNone(line.taxable_value)
        self.assertIsNone(line.effective_unit_cost_ex_tax)
        self.assertIsNone(po.total_taxable_value)

    def test_totals(self):
        po = self._po()
        self._line(po, quantity_ordered=2, unit_price=Decimal("100"), line_discount_amount=Decimal("5"))
        self._line(po, quantity_ordered=1, unit_price=Decimal("50"))
        self.assertEqual(po.total_gross_amount, Decimal("250.00"))
        self.assertEqual(po.total_discount_amount, Decimal("5.00"))
        self.assertEqual(po.total_discounted_amount, Decimal("245.00"))
        self.assertEqual(po.total_taxable_value, Decimal("245.00"))


class PurchaseOrderWorkflowTests(_POBase):
    def test_issue_stamps_audit(self):
        po = self._po()
        self._line(po)
        po = services.issue_purchase_order(po, self.user)
        self.assertEqual(po.status, PurchaseOrder.STATUS_ISSUED)
        self.assertEqual(po.issued_by, self.user)
        self.assertIsNotNone(po.issued_at)

    def _assert_issue_blocked(self, po, message):
        with self.assertRaisesMessage(services.PurchaseOrderError, message):
            services.issue_purchase_order(po, self.user)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_DRAFT)

    def test_issue_requires_lines(self):
        self._assert_issue_blocked(self._po(), "at least one line")

    def test_issue_requires_active_vendor(self):
        po = self._po()
        self._line(po)
        self.supplier.is_active = False
        self.supplier.save()
        self._assert_issue_blocked(po, "inactive")

    def test_issue_requires_active_warehouse(self):
        po = self._po()
        self._line(po)
        Warehouse.objects.filter(pk=self.warehouse.pk).update(is_active=False)
        self._assert_issue_blocked(po, "Warehouse")

    def test_issue_requires_active_variant(self):
        po = self._po()
        self._line(po)
        ProductVariant.objects.filter(pk=self.variant.pk).update(is_active=False)
        self._assert_issue_blocked(po, "variant is inactive")

    def test_issue_blocks_decant_line(self):
        po = self._po()
        # Bypasses clean(), e.g. a line created before a variant became a decant.
        self._line(po, variant=_variant("SKU-PO-DEC", is_decant=True))
        self._assert_issue_blocked(po, "decants cannot be purchased")

    def test_issue_requires_tax_rate_when_prices_include_tax(self):
        po = self._po(amounts_include_tax=True)
        self._line(po)
        self._assert_issue_blocked(po, "tax rate is required")

    def test_issue_only_from_draft(self):
        po = self._po()
        self._line(po)
        services.issue_purchase_order(po, self.user)
        with self.assertRaises(services.PurchaseOrderError):
            services.issue_purchase_order(po, self.user)

    def test_cancel_draft_and_issued(self):
        for issue_first in (False, True):
            with self.subTest(issued=issue_first):
                po = self._po()
                self._line(po)
                if issue_first:
                    services.issue_purchase_order(po, self.user)
                po = services.cancel_purchase_order(po, self.user, "Vendor out of stock")
                self.assertEqual(po.status, PurchaseOrder.STATUS_CANCELLED)
                self.assertEqual(po.cancelled_by, self.user)
                self.assertEqual(po.cancel_reason, "Vendor out of stock")
                self.assertIsNotNone(po.cancelled_at)

    def test_cancel_requires_reason(self):
        po = self._po()
        with self.assertRaisesMessage(services.PurchaseOrderError, "reason"):
            services.cancel_purchase_order(po, self.user, "   ")

    def test_final_and_receipt_statuses_cannot_transition(self):
        for status in (
            PurchaseOrder.STATUS_CANCELLED, PurchaseOrder.STATUS_PARTIALLY_RECEIVED,
            PurchaseOrder.STATUS_RECEIVED, PurchaseOrder.STATUS_CLOSED,
        ):
            with self.subTest(status=status):
                po = self._po(status=status)
                self._line(po)
                with self.assertRaises(services.PurchaseOrderError):
                    services.issue_purchase_order(po, self.user)
                with self.assertRaises(services.PurchaseOrderError):
                    services.cancel_purchase_order(po, self.user, "reason")

    def test_issued_po_displays_after_vendor_deactivated(self):
        po = self._po()
        self._line(po)
        services.issue_purchase_order(po, self.user)
        self.supplier.is_active = False
        self.supplier.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:purchases_purchaseorder_change", args=[po.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PO Vendor")


class PurchaseOrderInventoryTests(_POBase):
    def _snapshot(self):
        return (
            StockMovement.objects.count(),
            StockTransaction.objects.count(),
            list(InventoryStock.objects.order_by("pk").values_list("pk", "quantity", "quantity_reserved")),
        )

    def test_no_p1b_operation_changes_inventory(self):
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("5"),
        )
        before = self._snapshot()

        po = self._po()
        line = self._line(po)
        line.quantity_ordered = 20
        line.save()
        services.issue_purchase_order(po, self.user)
        services.cancel_purchase_order(po, self.user, "test")
        draft = self._po()
        self._line(draft)
        services.cancel_purchase_order(draft, self.user, "test")

        self.assertEqual(self._snapshot(), before)


class PurchaseOrderAdminTests(_POBase):
    def setUp(self):
        super().setUp()
        self.model_admin = django_admin.site._registry[PurchaseOrder]
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.user

    def test_registered_and_lines_not_registered(self):
        self.assertIsInstance(self.model_admin, PurchaseOrderAdmin)
        self.assertNotIn(PurchaseOrderLine, django_admin.site._registry)

    def test_delete_denied(self):
        self.assertFalse(self.model_admin.has_delete_permission(self.request, self._po()))

    def test_status_never_editable(self):
        po = self._po()
        for status, _ in PurchaseOrder.STATUS_CHOICES:
            po.status = status
            with self.subTest(status=status):
                self.assertIn("status", self.model_admin.get_readonly_fields(self.request, po))
        self.assertIn("status", self.model_admin.get_readonly_fields(self.request, None))

    def test_editable_header_fields_by_status(self):
        po = self._po()
        header = {
            "supplier", "warehouse", "order_date", "expected_date",
            "vendor_reference", "amounts_include_tax", "notes",
        }
        expected = {
            PurchaseOrder.STATUS_DRAFT: header,
            PurchaseOrder.STATUS_ISSUED: {"expected_date", "notes"},
            PurchaseOrder.STATUS_PARTIALLY_RECEIVED: {"expected_date", "notes"},
            PurchaseOrder.STATUS_RECEIVED: {"notes"},
            PurchaseOrder.STATUS_CLOSED: {"notes"},
            PurchaseOrder.STATUS_CANCELLED: {"notes"},
        }
        for status, editable in expected.items():
            po.status = status
            with self.subTest(status=status):
                readonly = set(self.model_admin.get_readonly_fields(self.request, po))
                self.assertEqual(header - readonly, editable)

    def test_lines_editable_only_in_draft(self):
        inline = PurchaseOrderLineInline(PurchaseOrder, django_admin.site)
        po = self._po()
        self.assertTrue(inline.has_add_permission(self.request, po))
        self.assertTrue(inline.has_change_permission(self.request, po))
        for status in (PurchaseOrder.STATUS_ISSUED, PurchaseOrder.STATUS_CANCELLED):
            po.status = status
            with self.subTest(status=status):
                self.assertFalse(inline.has_add_permission(self.request, po))
                self.assertFalse(inline.has_change_permission(self.request, po))
                self.assertFalse(inline.has_delete_permission(self.request, po))

    def test_add_and_change_pages_render(self):
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("admin:purchases_purchaseorder_add")).status_code, 200
        )
        po = self._po()
        self._line(po)
        response = self.client.get(reverse("admin:purchases_purchaseorder_change", args=[po.pk]))
        self.assertContains(response, po.po_number)

    def test_admin_add_sets_created_by(self):
        self.client.force_login(self.user)
        prefix = "lines"
        data = {
            "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_date": "2026-09-26", "notes": "", "vendor_reference": "Q-1",
            f"{prefix}-TOTAL_FORMS": "1", f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000",
            f"{prefix}-0-variant": self.variant.pk, f"{prefix}-0-quantity_ordered": "4",
            f"{prefix}-0-unit_price": "100.00", f"{prefix}-0-line_discount_amount": "0",
            "_save": "Save",
        }
        response = self.client.post(reverse("admin:purchases_purchaseorder_add"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context.get("errors"))
        po = PurchaseOrder.objects.get(vendor_reference="Q-1")
        self.assertEqual(po.created_by, self.user)
        self.assertEqual(po.lines.count(), 1)

    def test_issue_and_cancel_actions(self):
        self.client.force_login(self.user)
        po = self._po()
        self._line(po)
        issue_url = reverse("admin:purchases_purchaseorder_issue_po", args=[po.pk])
        # GET only shows the confirmation page; it never changes state.
        self.assertEqual(self.client.get(issue_url).status_code, 200)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_DRAFT)
        self.client.post(issue_url)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_ISSUED)

        url = reverse("admin:purchases_purchaseorder_cancel_po", args=[po.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"reason": ""})
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_ISSUED)
        self.client.post(url, {"reason": "Wrong vendor"})
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_CANCELLED)

    def test_actions_hidden_by_status(self):
        po = self._po()
        self.assertTrue(self.model_admin.has_issue_permission(self.request, po.pk))
        self.assertTrue(self.model_admin.has_cancel_permission(self.request, po.pk))
        PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.STATUS_CANCELLED)
        self.assertFalse(self.model_admin.has_issue_permission(self.request, po.pk))
        self.assertFalse(self.model_admin.has_cancel_permission(self.request, po.pk))

    def test_actions_require_custom_permissions(self):
        staff = User.objects.create_user(
            username="pobuyer", email="pobuyer@example.com", password="testpass123", is_staff=True,
        )
        staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="purchases",
                codename__in=["view_purchaseorder", "change_purchaseorder"],
            )
        )
        po = self._po()
        self._line(po)
        self.client.force_login(staff)
        response = self.client.post(reverse("admin:purchases_purchaseorder_issue_po", args=[po.pk]))
        self.assertEqual(response.status_code, 403)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_DRAFT)

        staff.user_permissions.add(Permission.objects.get(codename="issue_purchaseorder"))
        staff = User.objects.get(pk=staff.pk)
        self.client.force_login(staff)
        self.client.post(reverse("admin:purchases_purchaseorder_issue_po", args=[po.pk]))
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_ISSUED)

    def test_supplier_choices_exclude_other_inactive_vendors(self):
        inactive = Supplier.objects.create(name="Old Vendor", is_active=False)
        form = self.model_admin.get_form(self.request, None)
        self.assertNotIn(inactive, form.base_fields["supplier"].queryset)
        po = self._po(supplier=inactive)
        form = self.model_admin.get_form(self.request, po)
        self.assertIn(inactive, form.base_fields["supplier"].queryset)

    def test_purchase_orders_in_navigation(self):
        groups = {g.get("title"): g["items"] for g in settings.UNFOLD["SIDEBAR"]["navigation"]}
        items = {item["title"]: item for item in groups["Purchases"]}
        self.assertEqual(list(items), ["Vendors", "Purchase Orders"])
        self.assertEqual(
            str(items["Purchase Orders"]["link"]),
            reverse("admin:purchases_purchaseorder_changelist"),
        )

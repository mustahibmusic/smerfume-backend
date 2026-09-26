"""
Tests for apps.purchases (P1B — purchase orders).

    PurchaseOrderModelTests       — numbering, validation, DB constraints
    PurchaseOrderCalculationTests — gross / discounted / taxable / unit cost
    PurchaseOrderWorkflowTests    — issue / cancel services and transitions
    PurchaseOrderInventoryTests   — no P1B operation touches inventory
    PurchaseOrderAdminTests       — editability, actions, permissions, nav
    GoodsReceipt*Tests            — P1C receipts: posting, ledger, PO status,
                                    lifecycle, admin, concurrency
"""

import threading
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.inventory.models import InventoryStock, StockMovement, StockTransaction, Supplier, Warehouse
from apps.inventory.services import reservation as reservation_service
from apps.orders.models import Order, OrderItem
from apps.purchases import selectors, services
from apps.purchases.admin import (
    GoodsReceiptLineInline,
    PurchaseOrderAdmin,
    PurchaseOrderLineInline,
)
from apps.purchases.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine

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
        self.assertEqual(list(items), ["Vendors", "Purchase Orders", "Goods Receipts"])
        self.assertEqual(
            str(items["Goods Receipts"]["link"]),
            reverse("admin:purchases_goodsreceipt_changelist"),
        )
        self.assertEqual(
            str(items["Purchase Orders"]["link"]),
            reverse("admin:purchases_purchaseorder_changelist"),
        )


# --- P1C: goods receipts ---


class _GRNBase(_POBase):
    """An issued PO for 10 units of self.variant at 3100.00, 18% ex tax."""

    def setUp(self):
        super().setUp()
        self.po = self._po()
        self.po_line = self._line(self.po, quantity_ordered=10, tax_rate=Decimal("18"))
        services.issue_purchase_order(self.po, self.user)

    def _receipt(self, po=None):
        return services.create_receipt_from_po(po or self.po, self.user, vendor_document_reference="DC-1")

    def _grn_line(self, receipt, quantity, stock_type="retail", po_line=None):
        return GoodsReceiptLine.objects.create(
            receipt=receipt, po_line=po_line or self.po_line,
            stock_type=stock_type, quantity=quantity,
        )

    def _stock(self, stock_type="retail"):
        stock = InventoryStock.objects.filter(
            variant=self.variant, warehouse=self.warehouse, stock_type=stock_type
        ).first()
        return stock.quantity if stock else None

    def _post(self, receipt):
        return services.post_goods_receipt(receipt, self.user)


class GoodsReceiptModelTests(_GRNBase):
    def test_grn_number_and_draft_from_po(self):
        receipt = self._receipt()
        self.assertEqual(receipt.grn_number, f"GRN-{receipt.pk:05d}")
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_DRAFT)
        self.assertEqual(receipt.supplier, self.supplier)
        self.assertEqual(receipt.warehouse, self.warehouse)
        self.assertEqual(receipt.vendor_document_reference, "DC-1")
        self.assertEqual(receipt.lines.count(), 0)

    def test_receive_refused_for_draft_or_cancelled_po(self):
        draft = self._po()
        self._line(draft)
        with self.assertRaises(services.GoodsReceiptError):
            self._receipt(draft)
        services.cancel_purchase_order(draft, self.user, "x")
        with self.assertRaises(services.GoodsReceiptError):
            self._receipt(draft)

    def test_line_copies_variant_cost_and_tax_from_po_line(self):
        line = self._grn_line(self._receipt(), 3)
        self.assertEqual(line.variant, self.variant)
        self.assertEqual(line.unit_cost, Decimal("3100.0000"))
        self.assertEqual(line.tax_rate, Decimal("18.00"))

    def test_po_line_from_other_po_invalid(self):
        other = self._po()
        other_line = self._line(other)
        line = GoodsReceiptLine(receipt=self._receipt(), po_line=other_line, quantity=1)
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_db_constraints(self):
        receipt = self._receipt()
        for bad in (
            {"quantity": 0},
            {"stock_type": "tester"},
        ):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                self._grn_line(receipt, **{"quantity": 1, **bad})
        with self.assertRaises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.create(warehouse=self.warehouse, receipt_type="standard")
        with self.assertRaises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=receipt.pk).update(status="posted")


class GoodsReceiptPostingTests(_GRNBase):
    def test_post_adds_retail_and_damaged_stock_with_ledger(self):
        existing = InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("2"),
        )
        receipt = self._receipt()
        retail = self._grn_line(receipt, 8)
        damaged = self._grn_line(receipt, 1, "damaged")
        receipt = self._post(receipt)

        existing.refresh_from_db()
        self.assertEqual(existing.quantity, Decimal("10"))
        self.assertEqual(self._stock("damaged"), Decimal("1"))
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_POSTED)
        self.assertEqual(receipt.posted_by, self.user)
        txn = receipt.stock_transaction
        self.assertEqual(txn.transaction_type, StockTransaction.TYPE_PURCHASE_RECEIPT)
        movements = {m.source_receipt_line_id: m for m in txn.movements.all()}
        self.assertEqual(set(movements), {retail.pk, damaged.pk})
        for line in (retail, damaged):
            movement = movements[line.pk]
            self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_PURCHASE_IN)
            self.assertEqual(movement.reason, StockMovement.REASON_PURCHASE)
            self.assertEqual(movement.supplier, self.supplier)
            self.assertEqual(movement.quantity_delta, Decimal(line.quantity))
            self.assertEqual(movement.stock_type, line.stock_type)

    def test_shortage_not_received_and_po_partially_received(self):
        # Ordered 10: 8 good + 1 damaged arrived, 1 missing.
        receipt = self._receipt()
        self._grn_line(receipt, 8)
        self._grn_line(receipt, 1, "damaged")
        self._post(receipt)
        self.po.refresh_from_db()
        self.assertEqual(selectors.received_quantity(self.po_line), 9)
        self.assertEqual(selectors.outstanding_quantity(self.po_line), 1)
        self.assertEqual(self.po.status, PurchaseOrder.STATUS_PARTIALLY_RECEIVED)

        second = self._receipt()
        self._grn_line(second, 1)
        self._post(second)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.STATUS_RECEIVED)
        self.assertEqual(self._stock("retail"), Decimal("9"))

    def test_multiple_po_lines_including_same_variant(self):
        draft = self._po()
        first = self._line(draft, quantity_ordered=2, unit_price=Decimal("100"))
        second = self._line(draft, quantity_ordered=3, unit_price=Decimal("90"))
        services.issue_purchase_order(draft, self.user)
        receipt = self._receipt(draft)
        self._grn_line(receipt, 2, po_line=first)
        self._grn_line(receipt, 3, po_line=second)
        self._post(receipt)
        draft.refresh_from_db()
        self.assertEqual(draft.status, PurchaseOrder.STATUS_RECEIVED)
        self.assertEqual(self._stock(), Decimal("5"))
        costs = sorted(receipt.lines.values_list("unit_cost", flat=True))
        self.assertEqual(costs, [Decimal("90.0000"), Decimal("100.0000")])

    def test_over_receipt_blocked_and_rolled_back(self):
        receipt = self._receipt()
        self._grn_line(receipt, 8)
        self._grn_line(receipt, 3, "damaged")
        with self.assertRaisesMessage(services.GoodsReceiptError, "Over-receipt"):
            self._post(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_DRAFT)
        self.assertIsNone(self._stock())
        self.assertFalse(StockMovement.objects.exists())

    def test_over_receipt_across_receipts_blocked(self):
        first = self._receipt()
        self._grn_line(first, 7)
        self._post(first)
        second = self._receipt()
        self._grn_line(second, 4)
        with self.assertRaises(services.GoodsReceiptError):
            self._post(second)

    def test_double_post_refused_stock_added_once(self):
        receipt = self._receipt()
        self._grn_line(receipt, 5)
        self._post(receipt)
        with self.assertRaises(services.GoodsReceiptAlreadyPosted):
            self._post(receipt)
        self.assertEqual(self._stock(), Decimal("5"))
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_post_requires_lines(self):
        with self.assertRaisesMessage(services.GoodsReceiptError, "at least one"):
            self._post(self._receipt())

    def test_failure_mid_posting_rolls_back(self):
        receipt = self._receipt()
        self._grn_line(receipt, 5)
        with mock.patch(
            "apps.purchases.services.refresh_po_receipt_status", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self._post(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_DRAFT)
        self.assertIsNone(self._stock())
        self.assertFalse(StockTransaction.objects.exists())

    def test_cost_frozen_from_po_line_at_posting(self):
        receipt = self._receipt()
        line = self._grn_line(receipt, 2)
        # A tampered draft value is replaced by the PO line's cost.
        GoodsReceiptLine.objects.filter(pk=line.pk).update(unit_cost=Decimal("1"), tax_rate=None)
        self._post(receipt)
        line.refresh_from_db()
        self.assertEqual(line.unit_cost, Decimal("3100.0000"))
        self.assertEqual(line.tax_rate, Decimal("18.00"))

    def test_tax_inclusive_cost_snapshot(self):
        draft = self._po(amounts_include_tax=True)
        po_line = self._line(draft, quantity_ordered=2, unit_price=Decimal("1180"), tax_rate=Decimal("18"))
        services.issue_purchase_order(draft, self.user)
        receipt = self._receipt(draft)
        line = self._grn_line(receipt, 2, po_line=po_line)
        self._post(receipt)
        line.refresh_from_db()
        self.assertEqual(line.unit_cost, Decimal("1000.0000"))

    def test_post_refused_for_cancelled_po_and_mismatched_warehouse(self):
        receipt = self._receipt()
        self._grn_line(receipt, 1)
        other = Warehouse.objects.create(name="Other warehouse")
        GoodsReceipt.objects.filter(pk=receipt.pk).update(warehouse=other)
        with self.assertRaisesMessage(services.GoodsReceiptError, "warehouse"):
            self._post(receipt)
        GoodsReceipt.objects.filter(pk=receipt.pk).update(warehouse=self.warehouse)
        services.cancel_purchase_order(self.po, self.user, "vendor gone")
        with self.assertRaises(services.GoodsReceiptError):
            self._post(receipt)
        self.assertIsNone(self._stock())

    def test_only_receipt_variant_stock_changes(self):
        other_variant = _variant("SKU-PO-OTHER")
        other = InventoryStock.objects.create(
            variant=other_variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("4"),
        )
        receipt = self._receipt()
        self._grn_line(receipt, 3)
        self._post(receipt)
        other.refresh_from_db()
        self.assertEqual(other.quantity, Decimal("4"))
        self.assertEqual(other.quantity_reserved, Decimal("0"))


class GoodsReceiptLifecycleTests(_GRNBase):
    def test_cancel_draft_only(self):
        receipt = self._receipt()
        with self.assertRaises(services.GoodsReceiptError):
            services.cancel_goods_receipt(receipt, self.user, "")
        receipt = services.cancel_goods_receipt(receipt, self.user, "Wrong PO")
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_CANCELLED)
        self.assertEqual(receipt.cancelled_by, self.user)
        with self.assertRaises(services.GoodsReceiptError):
            self._post(receipt)

        posted = self._receipt()
        self._grn_line(posted, 1)
        self._post(posted)
        with self.assertRaises(services.GoodsReceiptError):
            services.cancel_goods_receipt(posted, self.user, "x")

    def test_po_cancel_allowed_with_draft_receipt_blocked_after_posted(self):
        self._grn_line(self._receipt(), 1)
        other = self._po()
        self._line(other)
        services.issue_purchase_order(other, self.user)
        posted = self._receipt(other)
        self._grn_line(posted, 1, po_line=other.lines.get())
        self._post(posted)
        # Blocked by the status rule (now partially received) ...
        with self.assertRaises(services.PurchaseOrderError):
            services.cancel_purchase_order(other, self.user, "x")
        # ... and by posted-receipt truth even if the status said issued.
        PurchaseOrder.objects.filter(pk=other.pk).update(status=PurchaseOrder.STATUS_ISSUED)
        with self.assertRaisesMessage(services.PurchaseOrderError, "posted goods receipts"):
            services.cancel_purchase_order(other, self.user, "x")
        services.cancel_purchase_order(self.po, self.user, "only drafts exist")

    def test_historical_receipt_viewable_after_deactivation(self):
        receipt = self._receipt()
        self._grn_line(receipt, 2)
        self._post(receipt)
        Supplier.objects.filter(pk=self.supplier.pk).update(is_active=False)
        ProductVariant.objects.filter(pk=self.variant.pk).update(is_active=False)
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]))
        self.assertContains(response, receipt.grn_number)
        self.assertContains(response, self.variant.sku)


class GoodsReceiptAdminTests(_GRNBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.user

    def test_add_page_disabled_and_delete_denied(self):
        model_admin = django_admin.site._registry[GoodsReceipt]
        self.assertFalse(model_admin.has_add_permission(self.request))
        self.assertFalse(model_admin.has_delete_permission(self.request, self._receipt()))
        self.assertNotIn(GoodsReceiptLine, django_admin.site._registry)

    def test_receive_goods_action_creates_empty_draft(self):
        url = reverse("admin:purchases_purchaseorder_receive_goods", args=[self.po.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(GoodsReceipt.objects.exists())
        response = self.client.post(url, {"received_date": "2026-09-26", "vendor_document_reference": "DC-9"})
        receipt = GoodsReceipt.objects.get()
        self.assertRedirects(
            response, reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(receipt.lines.count(), 0)

    def test_draft_page_offers_outstanding_lines_without_quantity(self):
        receipt = self._receipt()
        response = self.client.get(reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]))
        formset = response.context["inline_admin_formsets"][0].formset
        self.assertEqual(len(formset.forms), 1)
        self.assertEqual(formset.forms[0].initial.get("po_line"), self.po_line.pk)
        self.assertFalse(formset.forms[0].initial.get("quantity"))

    def _line_post_data(self, receipt, rows):
        response = self.client.get(reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]))
        data = {
            "received_date": "2026-09-26", "vendor_document_reference": "", "notes": "",
            "lines-TOTAL_FORMS": str(len(rows)), "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000", "_continue": "1",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"lines-{index}-{key}"] = value
        self.assertEqual(response.status_code, 200)
        return data

    def test_untouched_offered_rows_are_not_saved(self):
        receipt = self._receipt()
        data = self._line_post_data(receipt, [
            {"po_line": self.po_line.pk, "stock_type": "retail", "quantity": ""},
        ])
        # The row only carries its initial values, so it is unchanged.
        data["initial-lines-0-po_line"] = self.po_line.pk
        data["initial-lines-0-stock_type"] = "retail"
        self.client.post(reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]), data)
        self.assertEqual(receipt.lines.count(), 0)

    def test_staff_enters_split_lines(self):
        receipt = self._receipt()
        data = self._line_post_data(receipt, [
            {"po_line": self.po_line.pk, "stock_type": "retail", "quantity": "8"},
            {"po_line": self.po_line.pk, "stock_type": "damaged", "quantity": "1"},
        ])
        response = self.client.post(
            reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]), data
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(receipt.lines.values_list("stock_type", "quantity")),
            [("damaged", 1), ("retail", 8)],
        )

    def test_post_action_get_is_safe_and_post_posts(self):
        receipt = self._receipt()
        self._grn_line(receipt, 4)
        url = reverse("admin:purchases_goodsreceipt_post_grn", args=[receipt.pk])
        response = self.client.get(url)
        self.assertContains(response, "+4 to retail stock")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_DRAFT)
        self.assertIsNone(self._stock())
        self.client.post(url)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_POSTED)
        self.assertEqual(self._stock(), Decimal("4"))

    def test_posted_receipt_read_only(self):
        receipt = self._receipt()
        self._grn_line(receipt, 1)
        self._post(receipt)
        receipt.refresh_from_db()
        model_admin = django_admin.site._registry[GoodsReceipt]
        readonly = model_admin.get_readonly_fields(self.request, receipt)
        for field in ("received_date", "vendor_document_reference", "notes", "status"):
            self.assertIn(field, readonly)
        inline = GoodsReceiptLineInline(GoodsReceipt, django_admin.site)
        self.assertFalse(inline.has_add_permission(self.request, receipt))
        self.assertFalse(inline.has_change_permission(self.request, receipt))
        self.assertFalse(inline.has_delete_permission(self.request, receipt))
        self.assertFalse(model_admin.has_post_permission(self.request, receipt.pk))
        self.assertFalse(model_admin.has_cancel_permission(self.request, receipt.pk))

    def test_post_requires_permission(self):
        staff = User.objects.create_user(
            username="grnclerk", email="grnclerk@example.com", password="testpass123", is_staff=True,
        )
        staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="purchases",
            codename__in=["view_goodsreceipt", "change_goodsreceipt"],
        ))
        receipt = self._receipt()
        self._grn_line(receipt, 1)
        self.client.force_login(staff)
        url = reverse("admin:purchases_goodsreceipt_post_grn", args=[receipt.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertIsNone(self._stock())
        receive_url = reverse("admin:purchases_purchaseorder_receive_goods", args=[self.po.pk])
        self.assertEqual(self.client.post(receive_url, {"received_date": "2026-09-26"}).status_code, 403)


class GoodsReceiptConcurrencyTests(TransactionTestCase):
    """Real concurrent transactions (threads, separate DB connections)."""

    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="GRN Concurrency", is_default=True)
        self.user = User.objects.create_superuser(
            username="grnconc", email="grnconc@example.com", password="testpass123",
        )
        self.supplier = Supplier.objects.create(name="Concurrent Vendor")
        self.variant = _variant("SKU-GRN-CONC")
        self.po = PurchaseOrder.objects.create(supplier=self.supplier, warehouse=self.warehouse)
        self.po_line = PurchaseOrderLine.objects.create(
            purchase_order=self.po, variant=self.variant, quantity_ordered=10,
            unit_price=Decimal("100"),
        )
        services.issue_purchase_order(self.po, self.user)

    def _receipt_with(self, quantity, stock_type="retail"):
        receipt = services.create_receipt_from_po(self.po, self.user)
        GoodsReceiptLine.objects.create(
            receipt=receipt, po_line=self.po_line, stock_type=stock_type, quantity=quantity,
        )
        return receipt

    def _run_parallel(self, *calls):
        results = [None] * len(calls)
        barrier = threading.Barrier(len(calls))

        def _run(index, call):
            barrier.wait()
            try:
                call()
                results[index] = "ok"
            except services.GoodsReceiptError as exc:
                results[index] = type(exc).__name__
            except Exception as exc:  # deadlock/serialization: must roll back fully
                results[index] = f"db:{type(exc).__name__}: {exc}"
            finally:
                connection.close()

        threads = [threading.Thread(target=_run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def _retail(self):
        return InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )

    def test_same_receipt_posted_twice_concurrently(self):
        receipt = self._receipt_with(4)
        results = self._run_parallel(
            lambda: services.post_goods_receipt(receipt, self.user),
            lambda: services.post_goods_receipt(receipt, self.user),
        )
        self.assertEqual(sorted(results), ["GoodsReceiptAlreadyPosted", "ok"])
        self.assertEqual(self._retail().quantity, Decimal("4"))
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockTransaction.objects.count(), 1)

    def test_competing_receipts_cannot_over_receive(self):
        first, second = self._receipt_with(6), self._receipt_with(6)
        results = self._run_parallel(
            lambda: services.post_goods_receipt(first, self.user),
            lambda: services.post_goods_receipt(second, self.user),
        )
        self.assertEqual(sorted(results), ["GoodsReceiptError", "ok"])
        self.assertEqual(self._retail().quantity, Decimal("6"))
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_missing_stock_row_created_once_by_concurrent_posts(self):
        # Two POs so the PO lock does not serialise the two postings; both
        # must create the same missing retail + damaged rows.
        other_po = PurchaseOrder.objects.create(supplier=self.supplier, warehouse=self.warehouse)
        other_line = PurchaseOrderLine.objects.create(
            purchase_order=other_po, variant=self.variant, quantity_ordered=5,
            unit_price=Decimal("100"),
        )
        services.issue_purchase_order(other_po, self.user)
        first = self._receipt_with(3)
        GoodsReceiptLine.objects.create(
            receipt=first, po_line=self.po_line, stock_type="damaged", quantity=1,
        )
        second = services.create_receipt_from_po(other_po, self.user)
        GoodsReceiptLine.objects.create(receipt=second, po_line=other_line, quantity=2)
        GoodsReceiptLine.objects.create(
            receipt=second, po_line=other_line, stock_type="damaged", quantity=1,
        )
        results = self._run_parallel(
            lambda: services.post_goods_receipt(first, self.user),
            lambda: services.post_goods_receipt(second, self.user),
        )
        self.assertEqual(results, ["ok", "ok"])
        rows = InventoryStock.objects.filter(variant=self.variant, warehouse=self.warehouse)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(self._retail().quantity, Decimal("5"))
        self.assertEqual(rows.get(stock_type="damaged").quantity, Decimal("2"))
        self.assertEqual(StockMovement.objects.count(), 4)

    def test_receipt_and_checkout_reservation_on_same_stock_row(self):
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("5"),
        )
        order = Order.objects.create(
            user=self.user, order_number="SMR-GRN-CONC-1",
            subtotal=Decimal("0"), total=Decimal("0"),
        )
        order_item = OrderItem.objects.create(
            order=order, variant=self.variant, quantity=3, unit_price=Decimal("4500"),
        )
        receipt = self._receipt_with(4)
        results = self._run_parallel(
            lambda: services.post_goods_receipt(receipt, self.user),
            lambda: reservation_service.reserve_for_order_item(order_item, self.warehouse),
        )
        self.assertEqual(results, ["ok", "ok"])
        stock = self._retail()
        self.assertEqual(stock.quantity, Decimal("9"))
        self.assertEqual(stock.quantity_reserved, Decimal("3"))

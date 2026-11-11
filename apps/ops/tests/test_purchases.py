"""Smerfume Panel Purchases group: vendors, purchase orders, goods
receipts, supplier payments (DEC-010/011), Paid Through accounts,
discrepancies and booked quantity history. Writes go through
apps.purchases.services; action availability comes from
apps.purchases.rules, shared with Django Admin."""

import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.inventory.models import InventoryStock, Supplier, Warehouse
from apps.ops.access import OPS_ACCESS_PERMISSION
from apps.ops.tests.test_ops import PASSWORD, _perm
from apps.purchases import services
from apps.purchases.models import (
    BookedQuantityChange,
    GoodsReceipt,
    GoodsReceiptLine,
    PaymentAccount,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderPayment,
    ReceiptDiscrepancy,
    SupplierPaymentAttachment,
)

User = get_user_model()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _user(username, *codes):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password=PASSWORD)
    user.user_permissions.add(*(_perm(code) for code in (OPS_ACCESS_PERMISSION, *codes)))
    return user


def _variant(sku):
    brand = Brand.objects.get_or_create(name="OpsPOBrand", slug="opspobrand")[0]
    category = Category.objects.get_or_create(name="OpsPOCat", slug="opspocat")[0]
    product = Product.objects.get_or_create(
        name="OpsPOProduct", slug="opspoproduct", defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="opspoproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition, size_ml=100, selling_price="4000.00", mrp="4500.00", sku=sku,
    )


class _Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(username="poowner", email="poowner@example.com", password=PASSWORD)
        self.supplier = Supplier.objects.create(name="Ops Vendor")
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _variant("OPS-PO-100")
        self.account = PaymentAccount.objects.create(name="Ops Bank", kind=PaymentAccount.KIND_BANK)

    def _po(self, issue=True, quantity=10, price="1000", **kwargs):
        kwargs.setdefault("purchase_tax_treatment", PurchaseOrder.TAX_NO_GST_INVOICE)
        po = PurchaseOrder.objects.create(
            supplier=self.supplier, warehouse=self.warehouse, created_by=self.owner, **kwargs,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, variant=self.variant, quantity_ordered=quantity, unit_price=Decimal(price),
        )
        if issue:
            services.issue_purchase_order(po, self.owner)
            po.refresh_from_db()
        return po

    def _receipt(self, po, quantity=4):
        receipt = services.create_receipt_from_po(po, self.owner, vendor_document_reference="DC-1")
        GoodsReceiptLine.objects.create(
            receipt=receipt, po_line=po.lines.get(), stock_type="retail", quantity=quantity,
        )
        return receipt


class PurchasesNavigationAndAccessTests(_Base):
    def test_purchases_pages_live_and_others_placeholders(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("ops:po_list"))
        groups = {g["key"]: g for g in response.context["ops_nav"]["groups"]}
        purchases = groups["purchases"]
        self.assertTrue(purchases["open"])
        self.assertTrue(all(item["implemented"] for item in purchases["items"]))
        self.assertEqual(len(purchases["items"]), 7)
        for key in ("catalog", "customers"):
            self.assertFalse(any(i["implemented"] for i in groups[key]["items"]), key)
        self.assertNotIn('data-coming-soon="purchase_orders"', response.content.decode())

    def test_each_page_needs_its_view_permission(self):
        po = self._po()
        receipt = self._receipt(po)
        pages = {
            "inventory.view_supplier": [
                reverse("ops:vendor_list"), reverse("ops:vendor_detail", args=[self.supplier.vendor_code]),
            ],
            "purchases.view_purchaseorder": [reverse("ops:po_list"), reverse("ops:po_detail", args=[po.po_number])],
            "purchases.view_goodsreceipt": [
                reverse("ops:grn_list"), reverse("ops:grn_detail", args=[receipt.grn_number]),
            ],
            "purchases.view_purchaseorderpayment": [reverse("ops:payment_list")],
            "purchases.view_paymentaccount": [reverse("ops:account_list")],
            "purchases.view_receiptdiscrepancy": [reverse("ops:discrepancy_list")],
            "purchases.view_bookedquantitychange": [reverse("ops:booked_list")],
        }
        self.client.force_login(_user("nobody", "orders.view_order"))
        for urls in pages.values():
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403, url)
        for index, (perm, urls) in enumerate(pages.items()):
            self.client.force_login(_user(f"pviewer{index}", perm))
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_payment_data_needs_payment_view_permission(self):
        po = self._po()
        self.client.force_login(_user("poview", "purchases.view_purchaseorder"))
        list_page = self.client.get(reverse("ops:po_list"))
        self.assertFalse(list_page.context["can_view_payments"])
        self.assertNotContains(list_page, "Net paid")
        detail = self.client.get(reverse("ops:po_detail", args=[po.po_number]))
        self.assertIsNone(detail.context["payments"])
        self.assertNotContains(detail, "Supplier payments")


class PurchaseOrderActionTests(_Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_issue_through_service_and_rule(self):
        po = self._po(issue=False)
        detail = self.client.get(reverse("ops:po_detail", args=[po.po_number]))
        self.assertTrue(detail.context["actions"]["issue"])
        self.assertEqual(self.client.get(reverse("ops:po_issue", args=[po.po_number])).status_code, 200)
        response = self.client.post(reverse("ops:po_issue", args=[po.po_number]))
        self.assertRedirects(response, reverse("ops:po_detail", args=[po.po_number]))
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_ISSUED)
        # No longer a draft: the rule refuses the page and the POST.
        self.assertEqual(self.client.get(reverse("ops:po_issue", args=[po.po_number])).status_code, 403)
        self.assertEqual(self.client.post(reverse("ops:po_issue", args=[po.po_number])).status_code, 403)

    def test_cancel_needs_reason_and_permission(self):
        po = self._po()
        url = reverse("ops:po_cancel", args=[po.po_number])
        response = self.client.post(url, {"reason": ""})
        self.assertEqual(response.status_code, 200)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_ISSUED)
        self.client.post(url, {"reason": "Vendor out of stock"})
        po.refresh_from_db()
        self.assertEqual((po.status, po.cancel_reason), (PurchaseOrder.STATUS_CANCELLED, "Vendor out of stock"))

        other = self._po()
        self.client.force_login(_user("noissue", "purchases.view_purchaseorder"))
        self.assertEqual(
            self.client.post(reverse("ops:po_cancel", args=[other.po_number]), {"reason": "x"}).status_code, 403,
        )

    def test_close_after_partial_receipt(self):
        po = self._po()
        services.post_goods_receipt(self._receipt(po, quantity=4), self.owner)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_PARTIALLY_RECEIVED)
        self.client.post(reverse("ops:po_close", args=[po.po_number]), {"reason": "Vendor short-shipped"})
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.STATUS_CLOSED)

    def test_confirm_booked_quantities(self):
        po = self._po()
        line = po.lines.get()
        url = reverse("ops:po_confirm_booked", args=[po.po_number])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        data = {
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000", "lines-0-line": str(line.pk),
            "lines-0-original_total": "0", "lines-0-confirmed_total": "6", "note": "",
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)  # note required
        self.assertFalse(BookedQuantityChange.objects.exists())
        data["note"] = "Confirmed on call"
        self.assertRedirects(self.client.post(url, data), reverse("ops:po_detail", args=[po.po_number]))
        line.refresh_from_db()
        self.assertEqual(line.confirmed_booked_quantity, 6)
        change = BookedQuantityChange.objects.get()
        self.assertEqual((change.old_quantity, change.new_quantity, change.performed_by), (0, 6, self.owner))
        self.assertContains(self.client.get(reverse("ops:booked_list")), "Confirmed on call")

    def test_set_tax_treatment_for_unclassified_po(self):
        po = self._po()
        PurchaseOrder.objects.filter(pk=po.pk).update(purchase_tax_treatment=None)
        url = reverse("ops:po_tax_treatment", args=[po.po_number])
        self.client.post(url, {"treatment": PurchaseOrder.TAX_NO_GST_INVOICE})
        po.refresh_from_db()
        self.assertEqual(po.purchase_tax_treatment, PurchaseOrder.TAX_NO_GST_INVOICE)
        self.assertEqual(self.client.get(url).status_code, 403)  # one-time only


class SupplierPaymentTests(_Base):
    def setUp(self):
        super().setUp()
        self.po = self._po(quantity=1, price="1000")
        self.new_url = reverse("ops:po_payment_new", args=[self.po.po_number])
        self.media = tempfile.mkdtemp()
        patcher = mock.patch.object(
            SupplierPaymentAttachment._meta.get_field("file"), "storage", FileSystemStorage(location=self.media),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.media, True)

    def _data(self, **extra):
        data = {
            "amount": "400.00", "method": "upi", "entry_date": timezone.localdate().isoformat(),
            "reference": "UTR-1", "notes": "", "paid_through": str(self.account.pk),
        }
        data.update(extra)
        return data

    def test_draft_then_post_keeps_dec011_semantics(self):
        clerk = _user("clerk", "purchases.view_purchaseorder", "purchases.view_purchaseorderpayment",
                      "purchases.record_po_payment")
        self.client.force_login(clerk)
        page = self.client.get(self.new_url)
        self.assertEqual(page.context["form"].initial["amount"], Decimal("1000.00"))
        # Drafting is allowed, posting is not.
        self.client.post(self.new_url, self._data(_draft="1"))
        draft = PurchaseOrderPayment.objects.get()
        self.assertEqual((draft.status, draft.payment_number), (PurchaseOrderPayment.STATUS_DRAFT, None))
        self.po.refresh_from_db()
        self.assertEqual(self.po.net_paid, Decimal("0"))  # drafts never count
        entry_url = reverse("ops:payment_entry", args=[draft.public_id])
        self.assertEqual(self.client.post(entry_url, self._data(_post="1")).status_code, 403)

        poster = _user("poster", "purchases.view_purchaseorder", "purchases.view_purchaseorderpayment",
                       "purchases.post_po_payment")
        self.client.force_login(poster)
        response = self.client.post(entry_url, self._data(_post="1"))
        self.assertRedirects(response, reverse("ops:po_detail", args=[self.po.po_number]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, PurchaseOrderPayment.STATUS_POSTED)
        self.assertTrue(draft.payment_number)
        self.assertEqual(self.po.net_paid, Decimal("400.00"))
        # Posted entries are read-only.
        self.client.post(entry_url, self._data(_post="1", amount="900"))
        draft.refresh_from_db()
        self.assertEqual(draft.amount, Decimal("400.00"))

    def test_post_requires_active_paid_through(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.new_url, self._data(_post="1", paid_through=""))
        self.assertEqual(response.status_code, 200)
        self.assertIn("paid_through", response.context["form"].errors)
        self.assertFalse(PurchaseOrderPayment.objects.exists())

    def test_overpayment_refused_by_service(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.new_url, self._data(_post="1", amount="1000.01"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertFalse(PurchaseOrderPayment.objects.posted().exists())

    def test_delete_draft_and_attachments(self):
        self.client.force_login(self.owner)
        proof = SimpleUploadedFile("proof.pdf", PDF, content_type="application/pdf")
        self.client.post(self.new_url, {**self._data(_draft="1"), "attachments": proof})
        draft = PurchaseOrderPayment.objects.get()
        attachment = draft.attachments.get()
        download = self.client.get(reverse("ops:payment_attachment", args=[attachment.public_id]))
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment;", download["Content-Disposition"])
        self.assertEqual(download["Cache-Control"], "private, no-store")
        self.client.force_login(_user("noproof", "purchases.view_purchaseorder"))
        self.assertEqual(
            self.client.get(reverse("ops:payment_attachment", args=[attachment.public_id])).status_code, 403,
        )
        self.client.force_login(self.owner)
        entry_url = reverse("ops:payment_entry", args=[draft.public_id])
        self.client.post(entry_url, {"_remove_attachment": str(attachment.public_id)})
        self.assertFalse(draft.attachments.exists())
        response = self.client.post(entry_url, {"_delete": "1"})
        self.assertRedirects(response, reverse("ops:po_detail", args=[self.po.po_number]))
        self.assertFalse(PurchaseOrderPayment.objects.exists())

    def test_vendor_refund_and_reversal(self):
        self.client.force_login(self.owner)
        self.client.post(self.new_url, self._data(_post="1", amount="600.00"))
        refund_url = reverse("ops:po_vendor_refund", args=[self.po.po_number])
        self.client.post(refund_url, {
            "amount": "100.00", "entry_date": timezone.localdate().isoformat(), "method": "upi",
            "reference": "UTR-R", "notes": "",
        })
        self.assertEqual(self.po.net_paid, Decimal("500.00"))
        payment = PurchaseOrderPayment.objects.get(entry_type=PurchaseOrderPayment.TYPE_PAYMENT)
        refund = PurchaseOrderPayment.objects.get(entry_type=PurchaseOrderPayment.TYPE_VENDOR_REFUND)
        reverse_url = reverse("ops:po_reverse_entry", args=[self.po.po_number])
        # Reversing the payment would leave net paid below zero: refused.
        self.client.post(reverse_url, {"entry": str(payment.pk), "reason": "Wrong amount"})
        self.assertFalse(PurchaseOrderPayment.objects.filter(reverses=payment).exists())
        self.client.post(reverse_url, {"entry": str(refund.pk), "reason": "Refund never arrived"})
        self.assertTrue(PurchaseOrderPayment.objects.filter(
            entry_type=PurchaseOrderPayment.TYPE_REVERSAL, reverses=refund,
        ).exists())
        self.assertEqual(self.po.net_paid, Decimal("600.00"))
        ledger = self.client.get(reverse("ops:payment_list"), {"entry_type": "reversal"})
        self.assertEqual(len(ledger.context["object_list"]), 1)

    def test_payment_actions_follow_shared_rules(self):
        unpayable = self._po(issue=False)
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse("ops:po_payment_new", args=[unpayable.po_number])).status_code, 403,
        )
        detail = self.client.get(reverse("ops:po_detail", args=[unpayable.po_number]))
        self.assertFalse(detail.context["actions"]["record_payment"])


class GoodsReceiptTests(_Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.po = self._po()

    def _stock_qty(self):
        stock = InventoryStock.objects.filter(variant=self.variant, stock_type="retail").first()
        return stock.quantity if stock else Decimal("0")

    def test_post_cancel_and_reverse(self):
        receipt = self._receipt(self.po, quantity=4)
        detail = reverse("ops:grn_detail", args=[receipt.grn_number])
        self.assertTrue(self.client.get(detail).context["can_post"])
        self.client.post(reverse("ops:grn_post", args=[receipt.grn_number]))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_POSTED)
        self.assertEqual(self._stock_qty(), Decimal("4"))

        reverse_url = reverse("ops:grn_reverse", args=[receipt.grn_number])
        self.assertEqual(self.client.get(reverse_url).status_code, 200)
        line = receipt.lines.get()
        response = self.client.post(reverse_url, {f"line_{line.pk}": "1", "reason": "Counted wrong"})
        reversal = GoodsReceipt.objects.get(reverses=receipt)
        self.assertRedirects(response, reverse("ops:grn_detail", args=[reversal.grn_number]))
        self.assertEqual(self._stock_qty(), Decimal("3"))

        draft = self._receipt(self.po, quantity=1)
        self.client.post(reverse("ops:grn_cancel", args=[draft.grn_number]), {"reason": "Duplicate"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, GoodsReceipt.STATUS_CANCELLED)
        self.assertEqual(self._stock_qty(), Decimal("3"))

    def test_post_needs_permission(self):
        receipt = self._receipt(self.po)
        self.client.force_login(_user("grnview", "purchases.view_goodsreceipt"))
        self.assertFalse(self.client.get(reverse("ops:grn_detail", args=[receipt.grn_number])).context["can_post"])
        self.assertEqual(self.client.post(reverse("ops:grn_post", args=[receipt.grn_number])).status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.STATUS_DRAFT)


class DiscrepancyTests(_Base):
    def test_resolve_through_service(self):
        po = self._po()
        receipt = self._receipt(po, quantity=8)
        discrepancy = ReceiptDiscrepancy.objects.create(
            receipt=receipt, po_line=po.lines.get(), discrepancy_type=ReceiptDiscrepancy.TYPE_SHORT,
            quantity=2, created_by=self.owner,
        )
        services.post_goods_receipt(receipt, self.owner)
        self.client.force_login(_user("discview", "purchases.view_receiptdiscrepancy"))
        detail = reverse("ops:discrepancy_detail", args=[discrepancy.public_id])
        self.assertIsNone(self.client.get(detail).context["resolve_form"])
        resolve = reverse("ops:discrepancy_resolve", args=[discrepancy.public_id])
        self.assertEqual(self.client.post(resolve, {"resolution": "vendor_credit"}).status_code, 403)

        self.client.force_login(self.owner)
        resolution = ReceiptDiscrepancy.RESOLUTION_CHOICES[0][0]
        self.client.post(resolve, {"resolution": resolution, "notes": "Credit note"})
        discrepancy.refresh_from_db()
        self.assertEqual((discrepancy.status, discrepancy.resolved_by), (ReceiptDiscrepancy.STATUS_RESOLVED, self.owner))


class ReadOnlyPagesTests(_Base):
    def test_vendor_and_account_pages(self):
        self.client.force_login(self.owner)
        po = self._po()
        vendor_page = self.client.get(reverse("ops:vendor_detail", args=[self.supplier.vendor_code]))
        self.assertContains(vendor_page, po.po_number)
        self.assertEqual(self.client.get(f"/ops/purchases/vendors/{self.supplier.pk}/").status_code, 404)
        accounts = self.client.get(reverse("ops:account_list"))
        self.assertContains(accounts, "Ops Bank")
        self.assertNotContains(accounts, 'action="/ops/purchases/paid-through')

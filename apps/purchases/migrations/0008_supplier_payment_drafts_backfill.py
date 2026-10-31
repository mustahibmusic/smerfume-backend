# DEC-011 data migration. Existing rows are already posted (0007 default).
# Payments get SPAY numbers in (entry_date, id) order; posted_by/posted_at
# come from recorded_by/created_at; Paid Through stays NULL ("Not recorded").
# Amounts, types and links are not touched, so every total is unchanged.

from django.db import migrations


def backfill(apps, schema_editor):
    Payment = apps.get_model("purchases", "PurchaseOrderPayment")
    rows = Payment.objects.all().order_by("entry_date", "id")
    number = 0
    for row in rows.iterator():
        row.posted_by_id = row.recorded_by_id
        row.posted_at = row.created_at
        fields = ["posted_by", "posted_at"]
        if row.entry_type == "payment":
            number += 1
            row.payment_number = f"SPAY-{number:06d}"
            fields.append("payment_number")
        row.save(update_fields=fields)
    with schema_editor.connection.cursor() as cursor:
        if number:
            cursor.execute("SELECT setval('purchases_spay_seq', %s, true)", [number])
        else:
            cursor.execute("SELECT setval('purchases_spay_seq', 1, false)")


def unbackfill(apps, schema_editor):
    Payment = apps.get_model("purchases", "PurchaseOrderPayment")
    Payment.objects.update(payment_number=None, posted_by=None, posted_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("purchases", "0007_supplier_payment_drafts_schema"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]

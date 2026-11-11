"""DEC-018: customer-visible identity snapshot on OrderItem.

Adds the snapshot columns and backfills existing rows.

Backfill limitation: lines created before this migration never stored their
sale-time display values, so they can only be filled from the catalogue as it
is when this migration runs. Those rows are marked identity_backfilled=True.
If a product, edition or variant was renamed (or its SKU changed) before this
migration, the backfilled value shows the current name, not the one at sale
time. Rows that already have a snapshot are left untouched, and reversing the
migration drops the columns.
"""

from django.db import migrations, models

BATCH_SIZE = 500

SNAPSHOT_FIELDS = [
    "product_name_snapshot",
    "edition_name_snapshot",
    "variant_name_snapshot",
    "sku_snapshot",
    "size_ml_snapshot",
    "is_decant_snapshot",
    "concentration_snapshot",
    "identity_backfilled",
]


def _snapshot_values(variant):
    """Same output as apps.orders.models.identity_snapshot(). Historical
    models have no custom methods, so the catalogue display_name() formats
    are repeated here (Product, ProductEdition, ProductVariant)."""
    edition = variant.edition
    product = edition.product
    brand_name = product.brand.name
    product_name = f"{brand_name} {product.name}" if product.name else ""
    if edition.name:
        edition_display = f"{brand_name} {product.name} {edition.name}"
    else:
        edition_display = f"{brand_name} {product.name}"
    size = f"{variant.size_ml}ml"
    if variant.is_decant:
        size += " (Decant)"
    return {
        "product_name_snapshot": product_name,
        "edition_name_snapshot": edition.name or "",
        "variant_name_snapshot": f"{edition_display} {size}",
        "sku_snapshot": variant.sku,
        "size_ml_snapshot": variant.size_ml,
        "is_decant_snapshot": variant.is_decant,
        "concentration_snapshot": edition.get_concentration_display(),
        "identity_backfilled": True,
    }


def backfill_identity_snapshot(apps, schema_editor):
    OrderItem = apps.get_model("orders", "OrderItem")
    pending = (
        OrderItem.objects.filter(sku_snapshot="")
        .select_related("variant__edition__product__brand")
        .order_by("pk")
    )
    batch = []
    for item in pending.iterator(chunk_size=BATCH_SIZE):
        for field, value in _snapshot_values(item.variant).items():
            setattr(item, field, value)
        batch.append(item)
        if len(batch) >= BATCH_SIZE:
            OrderItem.objects.bulk_update(batch, SNAPSHOT_FIELDS)
            batch = []
    if batch:
        OrderItem.objects.bulk_update(batch, SNAPSHOT_FIELDS)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0010_productvariant_barcode"),
        ("orders", "0016_in_store_payment_separation"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="concentration_snapshot",
            field=models.CharField(blank=True, default="", editable=False, max_length=50),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="edition_name_snapshot",
            field=models.CharField(blank=True, default="", editable=False, max_length=100),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="identity_backfilled",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="is_decant_snapshot",
            field=models.BooleanField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="product_name_snapshot",
            field=models.CharField(blank=True, default="", editable=False, max_length=400),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="size_ml_snapshot",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="sku_snapshot",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="variant_name_snapshot",
            field=models.CharField(blank=True, default="", editable=False, max_length=600),
        ),
        migrations.RunPython(backfill_identity_snapshot, migrations.RunPython.noop),
    ]

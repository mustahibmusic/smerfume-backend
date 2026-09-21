from django.db import migrations


def backfill_sku(apps, schema_editor):
    ProductVariant = apps.get_model("catalog", "ProductVariant")
    for variant in ProductVariant.objects.filter(sku__isnull=True).order_by("id"):
        variant.sku = f"SMR-VAR-{variant.id:06d}"
        variant.save(update_fields=["sku"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_add_variant_sku_nullable_and_surcharge"),
    ]

    operations = [
        migrations.RunPython(backfill_sku, noop_reverse),
    ]

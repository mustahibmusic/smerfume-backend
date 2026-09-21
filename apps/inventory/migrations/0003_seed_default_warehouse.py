from django.db import migrations


def create_default_warehouse(apps, schema_editor):
    """Idempotent: only creates a default warehouse if none exists yet.
    Safe to run against an environment that already has one (e.g. created
    manually via admin before this migration ran)."""
    Warehouse = apps.get_model("inventory", "Warehouse")
    if Warehouse.objects.filter(is_default=True).exists():
        return
    Warehouse.objects.get_or_create(
        name="Default Warehouse",
        defaults={"is_default": True, "is_active": True},
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0002_stockreservation_stockreservationallocation_and_more"),
    ]

    operations = [
        migrations.RunPython(create_default_warehouse, noop_reverse),
    ]

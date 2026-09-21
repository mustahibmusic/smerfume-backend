from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0005_enforce_variant_sku_not_null"),
    ]

    operations = [
        TrigramExtension(),
    ]

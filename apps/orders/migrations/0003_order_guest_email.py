from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0002_alter_shippingaddress_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="guest_email",
            field=models.EmailField(blank=True, null=True),
        ),
    ]

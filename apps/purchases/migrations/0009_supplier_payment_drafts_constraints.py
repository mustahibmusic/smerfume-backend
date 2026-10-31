# DEC-011: constraints added after 0008 backfilled existing rows.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("purchases", "0008_supplier_payment_drafts_backfill"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseorderpayment",
            name="status",
            field=models.CharField(
                choices=[("draft", "Draft"), ("posted", "Posted")],
                default="draft",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseorderpayment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status", "posted"), ("entry_type", "payment"), _connector="OR"
                ),
                name="po_payment_draft_only_payment",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseorderpayment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("entry_type", "payment"),
                        ("status", "posted"),
                        ("payment_number__isnull", False),
                    ),
                    models.Q(
                        models.Q(
                            ("entry_type", "payment"),
                            ("status", "posted"),
                            _negated=True,
                        ),
                        ("payment_number__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="po_payment_number_iff_posted_payment",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseorderpayment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("payment_number__isnull", True),
                    ("payment_number__regex", "^SPAY-[0-9]{6,}$"),
                    _connector="OR",
                ),
                name="po_payment_number_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseorderpayment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("posted_at__isnull", True),
                        ("posted_by__isnull", True),
                        ("status", "draft"),
                    ),
                    models.Q(
                        ("posted_at__isnull", False),
                        ("posted_by__isnull", False),
                        ("status", "posted"),
                    ),
                    _connector="OR",
                ),
                name="po_payment_posted_audit",
            ),
        ),
    ]

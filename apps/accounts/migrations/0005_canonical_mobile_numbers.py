"""Convert stored local contact numbers to the canonical +91XXXXXXXXXX form.

Collisions (two accounts holding the same local number in different forms)
are skipped and left unchanged; they must be resolved with
resolve_mobile_collision first (docs/DEPLOYMENT.md). International and
unrecognised values are kept. Running it again changes nothing.
"""

from django.db import migrations

from apps.accounts.mobile_cleanup import canonicalize


def forwards(apps, schema_editor):
    result = canonicalize(apps.get_model("accounts", "User"))
    if result["collisions"]:
        print(
            f"\n  accounts.0005: skipped {result['collisions']} mobile collision(s); "
            "run audit_mobile_numbers, resolve them, then canonicalize_mobiles."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_auth_hardening"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

"""Convert recognised local contact numbers to +91XXXXXXXXXX.

    python manage.py canonicalize_mobiles

Same logic as migration accounts.0005_canonical_mobile_numbers, for
numbers left over because they were in a collision when the migration
ran. Collisions and international or unrecognised values are skipped.
Safe to run again.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.mobile_cleanup import canonicalize


class Command(BaseCommand):
    help = "Convert recognised local contact numbers to the canonical +91 form."

    def handle(self, *args, **options):
        with transaction.atomic():
            result = canonicalize(get_user_model())
        self.stdout.write(
            f"Updated {result['updated']}; already canonical {result['already']}; "
            f"skipped collisions {result['collisions']}; kept as they are {result['unrecognised']}."
        )
        if result["collisions"]:
            self.stdout.write(self.style.WARNING("Collisions remain: run audit_mobile_numbers."))

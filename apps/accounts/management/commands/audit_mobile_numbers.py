"""Read-only report on stored contact numbers before canonicalisation.

    python manage.py audit_mobile_numbers

Lists accounts whose numbers are the same local number in different forms
(collisions), with what each account holds, plus counts of numbers still
to convert and numbers kept as they are (international or unrecognised).
Mobile numbers are masked. Exits with status 1 while any collision exists.
Changes nothing.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.mobile import mask_mobile
from apps.accounts.mobile_cleanup import collisions, mobile_groups, pending
from apps.accounts.services.account_history import account_history


class Command(BaseCommand):
    help = "Report contact-number collisions and canonicalisation status (read-only)."

    def handle(self, *args, **options):
        User = get_user_model()
        groups, unrecognised = mobile_groups(User)
        clashing = collisions(groups)
        todo = pending(groups)

        self.stdout.write(f"Accounts with a recognised local number: {sum(len(r) for r in groups.values())}")
        self.stdout.write(f"Already canonical (+91): {sum(1 for r in groups.values() if len(r) == 1) - len(todo)}")
        self.stdout.write(f"To convert to +91: {len(todo)}")
        self.stdout.write(f"Kept as they are (international or unrecognised): {len(unrecognised)}")
        self.stdout.write(f"Collisions: {len(clashing)}")

        accounts = User.objects.in_bulk([pk for rows in clashing.values() for pk, _ in rows])
        for canonical, rows in sorted(clashing.items()):
            self.stdout.write(f"\nCollision on {mask_mobile(canonical)}:")
            for pk, stored in rows:
                account = accounts[pk]
                history = account_history(account)
                held = ", ".join(f"{name} {count}" for name, count in history.items() if count) or "no history"
                self.stdout.write(
                    f"  {account.public_id}  stored {mask_mobile(stored)} ({len(stored)} chars)  "
                    f"role {account.role}{'  STAFF' if account.is_staff or account.is_superuser else ''}  "
                    f"{'active' if account.is_active else 'inactive'}  "
                    f"last login {'yes' if account.last_login else 'never'}  {held}"
                )
        if clashing:
            raise CommandError(
                f"{len(clashing)} collision(s). Resolve each with resolve_mobile_collision before migrating."
            )

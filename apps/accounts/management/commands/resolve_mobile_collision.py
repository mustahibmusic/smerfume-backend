"""Explicitly resolve one contact-number collision.

    python manage.py resolve_mobile_collision --keep <public_id> --retire <public_id> [--dry-run]
    python manage.py resolve_mobile_collision --keep <public_id> --retire <public_id> --merge-orders [--dry-run]

Both accounts must hold the same local number (in any form), and the
retired account must be a customer account (never staff or superuser).

Default (strict): the retired account must have no history at all (no
orders, cart items, verified OTP logins, API sessions, admin log entries,
groups or permissions, and it never logged in). Nothing is moved.

--merge-orders (an explicit business decision): the retired account's
Orders move to the kept account; everything attached to those Orders
(items, payments, returns, refunds, delivery address) moves with them
because it belongs to the Order. Any other row pointing at the retired
account (OTP records, sessions, carts, profile, admin entries, groups...)
or a recorded login refuses the merge: those are never moved or guessed.

In both modes the retired account's number is cleared and it is
deactivated only after everything else succeeded, in one transaction with
both accounts locked. Output and logs show public ids, counts and masked
numbers only.
"""

import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.mobile import canonical_indian_mobile, mask_mobile
from apps.accounts.services.account_history import (
    MOVABLE_RELATION,
    account_history,
    direct_relations,
    has_history,
)
from apps.orders.models import Order

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Resolve one contact-number collision: retire a duplicate, optionally moving its orders."

    def add_arguments(self, parser):
        parser.add_argument("--keep", required=True, help="public_id of the account that keeps the number")
        parser.add_argument("--retire", required=True, help="public_id of the account to retire")
        parser.add_argument(
            "--merge-orders", action="store_true",
            help="Move the retired account's Orders to the kept account (business decision required)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Check and report only; change nothing")

    def handle(self, *args, keep, retire, merge_orders, dry_run, **options):
        User = get_user_model()
        if keep == retire:
            raise CommandError("--keep and --retire must be different accounts.")
        with transaction.atomic():
            locked = User.objects.select_for_update().filter(public_id__in=[keep, retire]).order_by("pk")
            accounts = {str(account.public_id): account for account in locked}
            if keep not in accounts or retire not in accounts:
                raise CommandError("Both accounts must exist.")
            kept, retired = accounts[keep], accounts[retire]
            number = canonical_indian_mobile(kept.mobile_number)
            if number is None or number != canonical_indian_mobile(retired.mobile_number):
                raise CommandError("The two accounts do not hold the same local number.")
            if retired.is_staff or retired.is_superuser or retired.role != "customer":
                raise CommandError("Only a customer account can be retired.")

            self.stdout.write(f"Number: {mask_mobile(number)}")
            self.stdout.write(f"Keep:   {kept.public_id} (stored {mask_mobile(kept.mobile_number)})")
            self.stdout.write(f"Retire: {retired.public_id} (stored {mask_mobile(retired.mobile_number)})")

            if merge_orders:
                order_ids = self._check_merge(kept, retired)
            else:
                order_ids = []
                if has_history(retired):
                    held = {name: count for name, count in account_history(retired).items() if count}
                    raise CommandError(
                        f"Refused: the account to retire has history {held or '(has logged in)'}. "
                        "Use --merge-orders only if the business decided to move its orders."
                    )

            if dry_run:
                self.stdout.write("Dry run: nothing changed.")
                return
            moved = Order.objects.filter(pk__in=order_ids, user=retired).update(user=kept)
            if moved != len(order_ids):
                raise CommandError("Orders changed while merging; nothing was saved.")
            retired.mobile_number = None
            retired.is_active = False
            retired.save(update_fields=["mobile_number", "is_active"])
        logger.info(
            "Mobile collision %s resolved: kept %s, retired %s, orders moved %s",
            mask_mobile(number), kept.public_id, retired.public_id, len(order_ids),
        )
        self.stdout.write(self.style.SUCCESS(
            f"Done: {len(order_ids)} order(s) moved; {retired.public_id} retired. Run audit_mobile_numbers again."
        ))

    def _check_merge(self, kept, retired):
        """Report both accounts' direct relations; refuse anything that
        cannot be moved safely. Returns the ids of the Orders to move."""
        if kept.is_staff or kept.is_superuser or kept.role != "customer" or not kept.is_active:
            raise CommandError("Orders can only move to an active customer account.")
        for label, account in (("keep", kept), ("retire", retired)):
            relations = direct_relations(account)
            text = ", ".join(f"{name}={count}" for name, count in sorted(relations.items())) or "none"
            self.stdout.write(f"Direct relations ({label}): {text}")
        blockers = {
            name: count for name, count in direct_relations(retired).items() if name != MOVABLE_RELATION
        }
        if retired.last_login is not None:
            blockers["last login recorded"] = 1
        order_ids = list(Order.objects.filter(user=retired).values_list("pk", flat=True))
        self.stdout.write(f"Orders to move: {len(order_ids)}")
        if blockers:
            text = ", ".join(f"{name}={count}" for name, count in sorted(blockers.items()))
            raise CommandError(f"Refused: the account to retire has history that is never moved: {text}.")
        self.stdout.write("Blockers: none")
        return order_ids

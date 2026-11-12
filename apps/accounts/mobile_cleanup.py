"""Bringing stored contact numbers to the canonical form.

Shared by the data migration (with the historical User model) and the
audit_mobile_numbers / canonicalize_mobiles / resolve_mobile_collision
commands. Accounts are never merged: two accounts whose numbers mean the
same local number are a collision, reported and left untouched until
staff retire one of them explicitly (resolve_mobile_collision).
"""

from collections import defaultdict

from .mobile import canonical_indian_mobile


def mobile_groups(User):
    """(groups, unrecognised) over every account with a mobile number.

    groups: {canonical +91 number: [(pk, stored value), ...]} for numbers
    recognised as local / +91 forms. unrecognised: [(pk, stored value)]
    for international or legacy values, which are kept as they are.
    """
    groups = defaultdict(list)
    unrecognised = []
    rows = User.objects.exclude(mobile_number__isnull=True).exclude(mobile_number="")
    for pk, stored in rows.order_by("pk").values_list("pk", "mobile_number"):
        canonical = canonical_indian_mobile(stored)
        if canonical is None:
            unrecognised.append((pk, stored))
        else:
            groups[canonical].append((pk, stored))
    return dict(groups), unrecognised


def collisions(groups):
    """Canonical numbers held by more than one account."""
    return {canonical: rows for canonical, rows in groups.items() if len(rows) > 1}


def pending(groups):
    """(pk, stored, canonical) for single-account numbers not yet canonical."""
    return [
        (rows[0][0], rows[0][1], canonical)
        for canonical, rows in groups.items()
        if len(rows) == 1 and rows[0][1] != canonical
    ]


def canonicalize(User):
    """Rewrite every recognised, collision-free number to its canonical
    form. Collisions and unrecognised values are skipped, never changed.
    Safe to run again: a second run changes nothing.

    Returns {"updated", "already", "collisions", "unrecognised"} counts.
    """
    groups, unrecognised = mobile_groups(User)
    clashing = collisions(groups)
    todo = pending(groups)
    for pk, _stored, canonical in todo:
        User.objects.filter(pk=pk).update(mobile_number=canonical)
    return {
        "updated": len(todo),
        "already": sum(1 for rows in groups.values() if len(rows) == 1) - len(todo),
        "collisions": len(clashing),
        "unrecognised": len(unrecognised),
    }

"""Read-only customer queries.

A customer account here is role "customer" with no admin flags (the same
test Django Admin and the Smerfume Panel use). Searches return active
accounts that have a mobile number, because an in-store sale is tied to
the customer's mobile. Only User fields are searched, never
CustomerProfile.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

NAME_MIN_LENGTH = 2
NAME_MAX_TERMS = 3
NAME_MAX_TERM_LENGTH = 50


def customer_accounts():
    User = get_user_model()
    return User.objects.filter(role="customer", is_staff=False, is_superuser=False)


def selectable_customers():
    """Active customer accounts with a mobile number."""
    return customer_accounts().filter(is_active=True, mobile_number__isnull=False).exclude(mobile_number="")


def search_customers(*, name=None, mobile_prefix=None, ids=None, limit):
    """Up to ``limit`` selectable customers, by exactly one criterion:

    - ``name``: every word (at most 3) is in the first or last name
      (case-insensitive contains); ordered by name.
    - ``mobile_prefix``: the stored full number starts with it (callers
      pass the selected country's dial code + typed digits, so the search
      never crosses countries); ordered by number.
    - ``ids``: these accounts, in the given order (recent customers).
    """
    given = [value is not None for value in (name, mobile_prefix, ids)]
    if sum(given) != 1:
        raise ValueError("Give exactly one of name, mobile_prefix or ids.")
    customers = selectable_customers()
    if ids is not None:
        by_id = customers.in_bulk(list(ids))
        return [by_id[pk] for pk in ids if pk in by_id][:limit]
    if mobile_prefix is not None:
        return list(customers.filter(mobile_number__startswith=mobile_prefix).order_by("mobile_number", "pk")[:limit])
    terms = [term[:NAME_MAX_TERM_LENGTH] for term in name.split()][:NAME_MAX_TERMS]
    for term in terms:
        customers = customers.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term))
    return list(customers.order_by("first_name", "last_name", "pk")[:limit])

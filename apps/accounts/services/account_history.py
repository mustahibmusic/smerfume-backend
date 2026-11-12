"""What an account has been used for, to decide whether a duplicate
customer account may be retired without losing anything."""

from django.contrib.auth import get_user_model

from apps.cart.models import CartItem
from apps.orders.models import Order

# The only direct relation --merge-orders may move to the kept account.
MOVABLE_RELATION = "orders.Order.user"


def account_history(account):
    """Counts of everything that ties the account to real activity.

    Unverified OTP requests and an empty customer profile are not history:
    they appear as soon as someone types the number.
    """
    return {
        "orders": Order.objects.filter(user=account).count(),
        "cart items": CartItem.objects.filter(cart__user=account).count(),
        "verified OTP logins": account.otps.filter(is_verified=True).count(),
        "API sessions": account.outstandingtoken_set.count(),
        "admin log entries": account.logentry_set.count(),
        "groups or permissions": account.groups.count() + account.user_permissions.count(),
    }


def has_history(account):
    return any(account_history(account).values()) or account.last_login is not None


def direct_relations(account):
    """Every row that points at the account directly, by "app.Model.field",
    including relations hidden from the model API (related_name="+"), plus
    group and permission memberships. Zero counts are left out."""
    User = get_user_model()
    found = {}
    for field in User._meta.get_fields(include_hidden=True):
        if not (field.auto_created and not field.concrete and (field.one_to_many or field.one_to_one)):
            continue
        count = field.related_model._base_manager.filter(**{field.field.name: account}).count()
        if count:
            found[f"{field.related_model._meta.label}.{field.field.name}"] = count
    for name in ("groups", "user_permissions"):
        count = getattr(account, name).count()
        if count:
            found[f"accounts.User.{name}"] = count
    return found

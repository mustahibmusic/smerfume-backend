"""Customer lookup for the New In-store Sale page (read-only JSON).

GET /ops/lookup/customers/?mobile=
    Customers whose canonical number starts with the typed local digits.
GET /ops/lookup/customers/<public_id>/last-address/
    The latest delivery address the customer used on an order, shown
    read-only when staff ask for it. It is never copied into a sale or
    saved as an account address.

Both need Smerfume Panel access and orders.add_order (the people who can
record an in-store sale). Responses carry public ids only, never database
ids, and a masked email. Requests are rate-limited per staff user.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.views import View

from apps.accounts.mobile import LOCAL_DIGITS, mobile_lookup_prefix
from apps.ops.mixins import OpsAccessMixin
from apps.ops.views.customers import customers_queryset
from apps.orders.models import ShippingAddress

logger = logging.getLogger(__name__)

ADD_ORDER = "orders.add_order"
CUSTOMER_LOOKUP_LIMIT = 5


def mask_email(email):
    """a***@g***.com: enough for staff to recognise, not to copy."""
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    name, dot, tld = domain.rpartition(".")
    if not dot:
        name, tld = domain, ""
    return f"{local[:1]}***@{name[:1]}***{'.' + tld if tld else ''}"


def _rate_limited(user):
    """True when this staff user went over OPS_LOOKUP_RATE_PER_MINUTE.
    Fails open if the cache is down (same policy as the DRF user throttle),
    and logs it."""
    key = f"ops-lookup:{user.pk}"
    try:
        cache.add(key, 0, timeout=60)
        return cache.incr(key) > settings.OPS_LOOKUP_RATE_PER_MINUTE
    except Exception:  # noqa: BLE001 - cache backend errors vary by backend
        logger.error("Ops lookup rate limit unavailable.", exc_info=True)
        return False


def _too_many():
    return JsonResponse({"error": "Too many lookups. Wait a moment and try again."}, status=429)


class CustomerLookupView(OpsAccessMixin, View):
    """GET /ops/lookup/customers/?mobile=<typed number>

    Local numbers only: at least 6 digits, taken as typed (a leading zero
    is part of the number); +91, 0 + 10 and 91 + 10 forms are recognised
    when complete. Another country code returns ``unsupported``.

    200 {"status": "ok" | "too_short" | "unsupported", "exact": bool,
         "truncated": bool, "results": [{"id", "name", "mobile", "email",
         "has_last_address"}]}

    Active customer accounts only (no staff), at most 5, by number.
    ``exact`` is true when the full 10-digit number matches one customer.
    Matches User.mobile_number only, never CustomerProfile.mobile.
    """

    permission_required = (ADD_ORDER,)
    http_method_names = ["get"]

    def get(self, request):
        if _rate_limited(request.user):
            return _too_many()
        raw = request.GET.get("mobile", "")[:20]
        prefix = mobile_lookup_prefix(raw)
        if prefix is None:
            unsupported = raw.strip().startswith("+") and not raw.strip().startswith("+91")
            return JsonResponse({
                "status": "unsupported" if unsupported else "too_short",
                "exact": False, "truncated": False, "results": [],
            })
        customers = list(
            customers_queryset().filter(is_active=True, mobile_number__startswith=prefix)
            .order_by("mobile_number", "pk")[:CUSTOMER_LOOKUP_LIMIT + 1]
        )
        truncated = len(customers) > CUSTOMER_LOOKUP_LIMIT
        customers = customers[:CUSTOMER_LOOKUP_LIMIT]
        with_address = set(
            ShippingAddress.objects.filter(order__user__in=customers)
            .values_list("order__user_id", flat=True).distinct()
        )
        full_number = len(prefix) == len("+91") + LOCAL_DIGITS
        exact = full_number and sum(1 for c in customers if c.mobile_number == prefix) == 1
        return JsonResponse({
            "status": "ok",
            "exact": exact,
            "truncated": truncated,
            "results": [
                {
                    "id": str(customer.public_id),
                    "name": customer.get_full_name(),
                    "mobile": customer.mobile_number,
                    "email": mask_email(customer.email),
                    "has_last_address": customer.pk in with_address,
                }
                for customer in customers
            ],
        })


class CustomerLastAddressView(OpsAccessMixin, View):
    """GET /ops/lookup/customers/<public_id>/last-address/

    200 {"address": {"full_name", "mobile", "address_line1",
         "address_line2", "city", "state", "pincode", "country"} | null}

    The delivery address of the customer's latest order that has one.
    Read-only; nothing is written. 404 for an unknown or non-customer id.
    """

    permission_required = (ADD_ORDER,)
    http_method_names = ["get"]

    def get(self, request, public_id):
        if _rate_limited(request.user):
            return _too_many()
        customer = customers_queryset().filter(public_id=public_id, is_active=True).first()
        if customer is None:
            raise Http404
        address = (
            ShippingAddress.objects.filter(order__user=customer)
            .order_by("-order__created_at", "-order__id").first()
        )
        if address is None:
            return JsonResponse({"address": None})
        return JsonResponse({"address": {
            "full_name": address.full_name,
            "mobile": address.mobile,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "state": address.get_state_display(),
            "pincode": address.pincode,
            "country": address.country,
        }})

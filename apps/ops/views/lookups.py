"""Customer lookup for the New In-store Sale page (read-only JSON).

GET /ops/lookup/customers/?recent=1 | ?name= | ?country_code=&mobile=
    Recent customers, name search, or number search within one country.
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

from apps.accounts import countries
from apps.accounts.mobile import mobile_search_prefix
from apps.accounts.selectors import NAME_MIN_LENGTH, customer_accounts, search_customers
from apps.ops.mixins import OpsAccessMixin
from apps.orders import selectors as order_selectors
from apps.orders.models import ShippingAddress

logger = logging.getLogger(__name__)

ADD_ORDER = "orders.add_order"
CUSTOMER_LOOKUP_LIMIT = 5
NAME_LOOKUP_LIMIT = 15


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
    """GET /ops/lookup/customers/ with exactly one mode:

    ``?recent=1``
        Customers on the most recent orders (the newest 200 orders are
        read), newest first, at most 15. Never the whole customer table.
    ``?name=<text>``
        At least 2 characters; every word (at most 3) in the first or last
        name. At most 15, by name.
    ``?country_code=%2B91&mobile=<national digits>``
        Prefix search on the stored full number, scoped to the selected
        dial code; at least 6 digits. At most 5, by number. ``exact`` is
        true when exactly one customer's stored number is the full typed
        number.

    200 {"mode", "status": "ok" | "too_short" | "invalid", "exact",
         "truncated", "results": [{"id", "name", "country", "country_code",
         "national_number", "mobile", "email", "has_last_address"}]}
    400 when no mode or more than one is given, or the country code is
    unknown.

    Active customer accounts with a mobile number only (no staff); User
    fields only, never CustomerProfile. Public ids, masked email.
    """

    permission_required = (ADD_ORDER,)
    http_method_names = ["get"]

    def get(self, request):
        params = request.GET
        modes = [mode for mode in ("recent", "name", "mobile") if mode in params]
        if len(modes) != 1 or ("country_code" in params) != (modes == ["mobile"]):
            return JsonResponse(
                {"error": "Use exactly one of ?recent=1, ?name= or ?country_code=&mobile=."}, status=400,
            )
        if _rate_limited(request.user):
            return _too_many()
        mode = modes[0]
        exact = False
        if mode == "recent":
            limit = NAME_LOOKUP_LIMIT
            customers = search_customers(ids=order_selectors.recent_customer_ids(limit=limit), limit=limit)
            return _results(mode, customers, limit=limit)
        if mode == "name":
            name = params.get("name", "").strip()[:100]
            if len(name) < NAME_MIN_LENGTH:
                return _empty(mode, "too_short")
            found = search_customers(name=name, limit=NAME_LOOKUP_LIMIT + 1)
            return _results(mode, found, limit=NAME_LOOKUP_LIMIT)
        dial_code = countries.clean_dial_code(params.get("country_code"))
        if dial_code is None:
            return JsonResponse({"error": "Unknown country code."}, status=400)
        prefix = mobile_search_prefix(dial_code, params.get("mobile", "")[:20])
        if prefix is None:
            return _empty(mode, "too_short")
        found = search_customers(mobile_prefix=prefix, limit=CUSTOMER_LOOKUP_LIMIT + 1)
        exact = sum(1 for customer in found if customer.mobile_number == prefix) == 1
        return _results(mode, found, limit=CUSTOMER_LOOKUP_LIMIT, exact=exact)


def _empty(mode, status):
    return JsonResponse({"mode": mode, "status": status, "exact": False, "truncated": False, "results": []})


def _results(mode, customers, *, limit, exact=False):
    truncated = len(customers) > limit
    customers = customers[:limit]
    with_address = set(
        ShippingAddress.objects.filter(order__user__in=customers)
        .values_list("order__user_id", flat=True).distinct()
    )
    rows = []
    for customer in customers:
        dial_code, national = countries.split_number(customer.mobile_number)
        rows.append({
            "id": str(customer.public_id),
            "name": customer.get_full_name(),
            "country": countries.country_for_dial(dial_code) if dial_code else "",
            "country_code": dial_code,
            "national_number": national,
            "mobile": customer.mobile_number,
            "email": mask_email(customer.email),
            "has_last_address": customer.pk in with_address,
        })
    return JsonResponse({"mode": mode, "status": "ok", "exact": exact, "truncated": truncated, "results": rows})


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
        customer = customer_accounts().filter(public_id=public_id, is_active=True).first()
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

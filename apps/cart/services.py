"""
Cart service layer.

Contains business logic that operates on Cart and CartItem models but is
called from outside the cart app (e.g. accounts.views on login).
"""

import logging
import re

from .models import Cart, CartItem

logger = logging.getLogger(__name__)

# Guest cart tokens are generated as uuid.uuid4().hex (apps.cart.views):
# exactly 32 lowercase hex characters.
_CART_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


class InvalidCartToken(Exception):
    """The X-Cart-Token header is present but not in the generated format."""

    message = "X-Cart-Token is not valid. Send the cart_token the API returned, or omit the header."

    def __init__(self):
        super().__init__(self.message)


def is_valid_cart_token(token) -> bool:
    return isinstance(token, str) and _CART_TOKEN_RE.fullmatch(token) is not None


def merge_guest_cart(cart_token: str, user) -> None:
    """Merge a guest cart into the authenticated user's cart on login.

    For each item in the guest cart:
    - If the same variant already exists in the user's cart, quantities are
      added (capped at 99).
    - Otherwise, the item is moved directly into the user's cart.

    The guest cart is deleted after the merge regardless of outcome.

    Args:
        cart_token: The guest cart session_key UUID string from the
                    X-Cart-Token request header.
        user: The authenticated User instance to merge into.

    A malformed token is ignored (no cart can have it), so login is never
    blocked by a bad header.
    """
    if not is_valid_cart_token(cart_token):
        return
    try:
        guest_cart = Cart.objects.prefetch_related("items__variant").get(
            session_key=cart_token, user__isnull=True
        )
    except Cart.DoesNotExist:
        logger.debug("merge_guest_cart: no guest cart found for token %s", cart_token)
        return

    guest_items = list(guest_cart.items.select_related("variant").all())
    if not guest_items:
        guest_cart.delete()
        return

    user_cart, _ = Cart.objects.get_or_create(user=user)

    for guest_item in guest_items:
        try:
            user_item = CartItem.objects.get(cart=user_cart, variant=guest_item.variant)
            user_item.quantity = min(user_item.quantity + guest_item.quantity, 99)
            user_item.save(update_fields=["quantity", "updated_at"])
        except CartItem.DoesNotExist:
            guest_item.cart = user_cart
            guest_item.save(update_fields=["cart", "updated_at"])

    guest_cart.delete()
    logger.info(
        "merge_guest_cart: merged %d item(s) from guest cart %s into user %s",
        len(guest_items),
        cart_token,
        user.pk,
    )

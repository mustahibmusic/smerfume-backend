"""
Cart service layer.

Contains business logic that operates on Cart and CartItem models but is
called from outside the cart app (e.g. accounts.views on login).
"""

import logging

from .models import Cart, CartItem

logger = logging.getLogger(__name__)


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
    """
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

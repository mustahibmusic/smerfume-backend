"""
Cart views.

Supports both authenticated users (JWT) and guests (X-Cart-Token header).

Cart resolution rules:
- Authenticated request (valid JWT):  cart is keyed by request.user.
- Guest request with X-Cart-Token:    cart is looked up by session_key.
- Guest request without X-Cart-Token: new cart is created; cart_token is
                                      returned in the response body so the
                                      client can store and re-use it.
- Guest request with a malformed X-Cart-Token (not 32 lowercase hex
  characters, the generated format): 400, before any database lookup.

Cart lines are addressed by CartItem.public_id; internal ids never appear.
"""

import uuid

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Cart, CartItem
from .serializers import AddToCartSerializer, CartSerializer, UpdateCartItemSerializer
from .services import InvalidCartToken, is_valid_cart_token


def _guest_cart_token(request):
    """The X-Cart-Token header, or None when absent or empty. Raises
    InvalidCartToken when it is not in the generated format."""
    token = request.headers.get("X-Cart-Token")
    if not token:
        return None
    if not is_valid_cart_token(token):
        raise InvalidCartToken()
    return token


class _CartTokenErrorMixin:
    """Answer a malformed X-Cart-Token with a 400 in the cart API's error
    format instead of letting it reach the database."""

    def handle_exception(self, exc):
        if isinstance(exc, InvalidCartToken):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


def _resolve_cart(request):
    """Return the Cart for the current request, creating one if needed.

    Args:
        request: DRF Request object.

    Returns:
        Cart instance.
    """
    if request.user.is_authenticated:
        cart, _ = Cart.objects.get_or_create(user=request.user)
        return cart

    cart_token = _guest_cart_token(request)
    if cart_token:
        cart, _ = Cart.objects.get_or_create(
            session_key=cart_token,
            defaults={"user": None},
        )
        return cart

    # New guest — generate a fresh token and create a cart
    new_token = uuid.uuid4().hex
    return Cart.objects.create(session_key=new_token)


def _get_cart_item(request, item_public_id):
    """Fetch a CartItem belonging to the current request's cart.

    Args:
        request: DRF Request object.
        item_public_id: public_id (UUID) of the CartItem.

    Returns:
        CartItem instance or None if not found.
    """
    try:
        if request.user.is_authenticated:
            return CartItem.objects.select_related("cart").get(
                public_id=item_public_id, cart__user=request.user
            )
        cart_token = _guest_cart_token(request)
        if not cart_token:
            return None
        return CartItem.objects.select_related("cart").get(
            public_id=item_public_id,
            cart__session_key=cart_token,
            cart__user__isnull=True,
        )
    except CartItem.DoesNotExist:
        return None


_CART_TOKEN_HEADER = OpenApiParameter(
    name="X-Cart-Token",
    type=str,
    location=OpenApiParameter.HEADER,
    required=False,
    description=(
        "Guest cart token: 32 lowercase hex characters, returned as `cart_token` "
        "in the cart response. Any other value is a 400. Omit when sending a JWT — "
        "the cart is resolved from the authenticated user and this header is ignored."
    ),
)


class CartDetailView(_CartTokenErrorMixin, APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Get cart",
        description=(
            "Return the current cart with all items and totals.\n\n"
            "- **Authenticated:** resolved from the JWT.\n"
            "- **Guest:** resolved from `X-Cart-Token` header. If omitted, a new "
            "  empty cart is created and its `cart_token` is returned — store it "
            "  and send it on every subsequent guest cart request."
        ),
        parameters=[_CART_TOKEN_HEADER],
        responses={
            200: CartSerializer,
        },
    )
    def get(self, request):
        cart = _resolve_cart(request)
        return Response(CartSerializer(cart).data)


class CartAddItemView(_CartTokenErrorMixin, APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Add item to cart",
        description=(
            "Add a product variant to the cart by its `public_id` (`variant_id`). "
            "The variant, its edition and its product must all be active. "
            "If the variant is already in the cart, quantities are combined "
            "(capped at 99 per line item).\n\n"
            "Supports both authenticated (JWT) and guest (`X-Cart-Token`) requests."
        ),
        parameters=[_CART_TOKEN_HEADER],
        request=AddToCartSerializer,
        responses={
            200: CartSerializer,
            400: OpenApiResponse(
                description="Unknown or unavailable variant, invalid quantity, or malformed X-Cart-Token."
            ),
        },
        examples=[
            OpenApiExample(
                "Add 2 units of a variant",
                request_only=True,
                value={"variant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "quantity": 2},
            )
        ],
    )
    def post(self, request):
        serializer = AddToCartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        cart = _resolve_cart(request)
        variant = serializer.validated_data["variant_id"]
        qty = serializer.validated_data["quantity"]

        item, created = CartItem.objects.get_or_create(
            cart=cart,
            variant=variant,
            defaults={"quantity": qty},
        )

        if not created:
            item.quantity = min(item.quantity + qty, 99)
            item.save(update_fields=["quantity", "updated_at"])

        return Response(CartSerializer(cart).data, status=status.HTTP_200_OK)


class CartItemView(_CartTokenErrorMixin, APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Update cart item quantity",
        description="Set a new quantity for a specific cart line item.",
        parameters=[
            _CART_TOKEN_HEADER,
            OpenApiParameter(
                "item_public_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
                description="public_id of the cart line (from the cart response).",
            ),
        ],
        request=UpdateCartItemSerializer,
        responses={
            200: CartSerializer,
            400: OpenApiResponse(description="Invalid quantity or malformed X-Cart-Token."),
            404: OpenApiResponse(description="Item not found in this cart."),
        },
    )
    def patch(self, request, item_public_id):
        item = _get_cart_item(request, item_public_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = UpdateCartItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item.quantity = serializer.validated_data["quantity"]
        item.save(update_fields=["quantity", "updated_at"])

        return Response(CartSerializer(item.cart).data)

    @extend_schema(
        tags=["Cart"],
        summary="Remove cart item",
        description="Remove a specific line item from the cart.",
        parameters=[
            _CART_TOKEN_HEADER,
            OpenApiParameter(
                "item_public_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
                description="public_id of the cart line (from the cart response).",
            ),
        ],
        responses={
            200: CartSerializer,
            404: OpenApiResponse(description="Item not found in this cart."),
        },
    )
    def delete(self, request, item_public_id):
        item = _get_cart_item(request, item_public_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        cart = item.cart
        item.delete()
        return Response(CartSerializer(cart).data)


class CartClearView(_CartTokenErrorMixin, APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Clear cart",
        description="Remove all items from the cart.",
        parameters=[_CART_TOKEN_HEADER],
        responses={
            200: CartSerializer,
        },
    )
    def delete(self, request):
        cart = _resolve_cart(request)
        cart.items.all().delete()
        return Response(CartSerializer(cart).data)

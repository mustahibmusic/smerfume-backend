"""
Cart views.

Supports both authenticated users (JWT) and guests (X-Cart-Token header).

Cart resolution rules:
- Authenticated request (valid JWT):  cart is keyed by request.user.
- Guest request with X-Cart-Token:    cart is looked up by session_key.
- Guest request without X-Cart-Token: new cart is created; cart_token is
                                      returned in the response body so the
                                      client can store and re-use it.
"""

import uuid

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

    cart_token = request.headers.get("X-Cart-Token")
    if cart_token:
        cart, _ = Cart.objects.get_or_create(
            session_key=cart_token,
            defaults={"user": None},
        )
        return cart

    # New guest — generate a fresh token and create a cart
    new_token = uuid.uuid4().hex
    return Cart.objects.create(session_key=new_token)


def _get_cart_item(request, item_id):
    """Fetch a CartItem belonging to the current request's cart.

    Args:
        request: DRF Request object.
        item_id: Primary key of the CartItem.

    Returns:
        CartItem instance or None if not found.
    """
    try:
        if request.user.is_authenticated:
            return CartItem.objects.select_related("cart").get(
                id=item_id, cart__user=request.user
            )
        cart_token = request.headers.get("X-Cart-Token")
        if not cart_token:
            return None
        return CartItem.objects.select_related("cart").get(
            id=item_id,
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
        "Guest cart token. Returned as `cart_token` in the cart response. "
        "Omit when sending a JWT — the cart is resolved from the authenticated user."
    ),
)


class CartDetailView(APIView):
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


class CartAddItemView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Add item to cart",
        description=(
            "Add a product variant to the cart. "
            "If the variant is already in the cart, quantities are combined "
            "(capped at 99 per line item).\n\n"
            "Supports both authenticated (JWT) and guest (`X-Cart-Token`) requests."
        ),
        parameters=[_CART_TOKEN_HEADER],
        request=AddToCartSerializer,
        responses={
            200: CartSerializer,
            400: OpenApiResponse(description="Invalid variant or quantity."),
        },
        examples=[
            OpenApiExample(
                "Add 2 units of a variant",
                request_only=True,
                value={"variant_id": 1, "quantity": 2},
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


class CartItemView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Update cart item quantity",
        description="Set a new quantity for a specific cart line item.",
        parameters=[
            _CART_TOKEN_HEADER,
            OpenApiParameter("item_id", int, OpenApiParameter.PATH, description="Cart item ID"),
        ],
        request=UpdateCartItemSerializer,
        responses={
            200: CartSerializer,
            400: OpenApiResponse(description="Invalid quantity."),
            404: OpenApiResponse(description="Item not found in this cart."),
        },
    )
    def patch(self, request, item_id):
        item = _get_cart_item(request, item_id)
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
            OpenApiParameter("item_id", int, OpenApiParameter.PATH, description="Cart item ID"),
        ],
        responses={
            200: CartSerializer,
            404: OpenApiResponse(description="Item not found in this cart."),
        },
    )
    def delete(self, request, item_id):
        item = _get_cart_item(request, item_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        cart = item.cart
        item.delete()
        return Response(CartSerializer(cart).data)


class CartClearView(APIView):
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

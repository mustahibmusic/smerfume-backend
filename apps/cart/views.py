from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Cart, CartItem
from .serializers import AddToCartSerializer, CartSerializer, UpdateCartItemSerializer


def _get_or_create_cart(request):
    """Return the cart for the authenticated user, creating it if needed."""
    cart, _ = Cart.objects.get_or_create(user=request.user)
    return cart


class CartDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Cart"],
        summary="Get cart",
        description="Return the authenticated user's current cart with all items and totals.",
        responses={
            200: CartSerializer,
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def get(self, request):
        cart = _get_or_create_cart(request)
        return Response(CartSerializer(cart).data)


class CartAddItemView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Cart"],
        summary="Add item to cart",
        description=(
            "Add a product variant to the cart. "
            "If the variant is already in the cart, the quantities are combined "
            "(capped at 99 per line item)."
        ),
        request=AddToCartSerializer,
        responses={
            200: CartSerializer,
            400: OpenApiResponse(description="Invalid variant or quantity."),
            401: OpenApiResponse(description="Access token missing or expired."),
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

        cart = _get_or_create_cart(request)
        variant = serializer.validated_data["variant_id"]
        qty = serializer.validated_data["quantity"]

        item, created = CartItem.objects.get_or_create(
            cart=cart,
            variant=variant,
            defaults={"quantity": qty},
        )

        if not created:
            item.quantity = min(item.quantity + qty, 99)
            item.save(update_fields=["quantity"])

        return Response(CartSerializer(cart).data, status=status.HTTP_200_OK)


class CartItemView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Cart"],
        summary="Update cart item quantity",
        description="Set a new quantity for a specific cart line item.",
        parameters=[
            OpenApiParameter("item_id", int, OpenApiParameter.PATH, description="Cart item ID"),
        ],
        request=UpdateCartItemSerializer,
        responses={
            200: CartSerializer,
            400: OpenApiResponse(description="Invalid quantity."),
            401: OpenApiResponse(description="Access token missing or expired."),
            404: OpenApiResponse(description="Item not found in this cart."),
        },
    )
    def patch(self, request, item_id):
        item = self._get_item(request, item_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = UpdateCartItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item.quantity = serializer.validated_data["quantity"]
        item.save(update_fields=["quantity"])

        return Response(CartSerializer(item.cart).data)

    @extend_schema(
        tags=["Cart"],
        summary="Remove cart item",
        description="Remove a specific line item from the cart.",
        parameters=[
            OpenApiParameter("item_id", int, OpenApiParameter.PATH, description="Cart item ID"),
        ],
        responses={
            200: CartSerializer,
            401: OpenApiResponse(description="Access token missing or expired."),
            404: OpenApiResponse(description="Item not found in this cart."),
        },
    )
    def delete(self, request, item_id):
        item = self._get_item(request, item_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        cart = item.cart
        item.delete()
        return Response(CartSerializer(cart).data)

    def _get_item(self, request, item_id):
        try:
            return CartItem.objects.select_related("cart").get(
                id=item_id, cart__user=request.user
            )
        except CartItem.DoesNotExist:
            return None


class CartClearView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Cart"],
        summary="Clear cart",
        description="Remove all items from the authenticated user's cart.",
        responses={
            200: CartSerializer,
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def delete(self, request):
        cart = _get_or_create_cart(request)
        cart.items.all().delete()
        return Response(CartSerializer(cart).data)

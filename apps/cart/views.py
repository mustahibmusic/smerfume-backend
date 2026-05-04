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
    """GET /api/cart/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        cart = _get_or_create_cart(request)
        serializer = CartSerializer(cart)
        return Response(serializer.data)


class CartAddItemView(APIView):
    """POST /api/cart/add/

    Body: { "variant_id": <pk>, "quantity": <int> }
    If the variant is already in the cart, quantity is incremented.
    """

    permission_classes = [IsAuthenticated]

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
    """
    PATCH /api/cart/items/{item_id}/ — update quantity
    DELETE /api/cart/items/{item_id}/ — remove item
    """

    permission_classes = [IsAuthenticated]

    def _get_item(self, request, item_id):
        try:
            return CartItem.objects.select_related("cart").get(
                id=item_id, cart__user=request.user
            )
        except CartItem.DoesNotExist:
            return None

    def patch(self, request, item_id):
        item = self._get_item(request, item_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = UpdateCartItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item.quantity = serializer.validated_data["quantity"]
        item.save(update_fields=["quantity"])

        return Response(CartSerializer(item.cart).data)

    def delete(self, request, item_id):
        item = self._get_item(request, item_id)
        if item is None:
            return Response({"error": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        cart = item.cart
        item.delete()
        return Response(CartSerializer(cart).data)


class CartClearView(APIView):
    """DELETE /api/cart/clear/"""

    permission_classes = [IsAuthenticated]

    def delete(self, request):
        cart = _get_or_create_cart(request)
        cart.items.all().delete()
        return Response(CartSerializer(cart).data)

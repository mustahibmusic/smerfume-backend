from django.conf import settings
from django.db import models

from apps.catalog.models import ProductVariant
from apps.core.models import BaseModel


class Cart(BaseModel):
    """One active cart per user; anonymous carts are keyed by session."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="cart",
    )
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True)

    class Meta:
        db_table = "cart_cart"
        constraints = [
            models.UniqueConstraint(
                fields=["session_key"],
                condition=models.Q(user__isnull=True),
                name="unique_anonymous_cart_session",
            ),
        ]

    def get_total(self):
        return sum(item.line_total for item in self.items.select_related("variant"))

    def __str__(self):
        owner = self.user.email if self.user else f"anon:{self.session_key}"
        return f"Cart({owner})"


class CartItem(BaseModel):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE)
    quantity = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = "cart_cartitem"
        unique_together = ("cart", "variant")

    @property
    def line_total(self):
        return self.variant.selling_price * self.quantity

    def __str__(self):
        return f"{self.quantity}× {self.variant}"

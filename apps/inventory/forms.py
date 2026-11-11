"""Staff forms for inventory workflows, shared by Django Admin and the
Smerfume Panel (/ops/). Input validation only: quantities, ledgers and
transfer rules stay in apps.inventory.services.adjustment, which re-checks
everything with the rows locked."""

from django import forms
from unfold.widgets import (
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextareaWidget,
)

from .models import InventoryStock, StockMovement
from .services import adjustment as adjustment_services

# Reasons a manual adjustment may record. Purchases come only from goods
# receipts and opening stock only from opening receipts.
MANUAL_ADJUSTMENT_REASONS = [
    choice for choice in StockMovement.REASON_CHOICES
    if choice[0] not in (StockMovement.REASON_PURCHASE, StockMovement.REASON_OPENING_STOCK)
]

TRANSFER_REASONS = (
    StockMovement.REASON_MARKETING_USE,
    StockMovement.REASON_DAMAGED_GOODS,
    StockMovement.REASON_STOCKTAKING_RESULTS,
    StockMovement.REASON_STOCK_WRITTEN_OFF,
)


class StockTypeTransferForm(forms.Form):
    to_type = forms.ChoiceField(label="Move to", widget=UnfoldAdminSelectWidget)
    quantity = forms.IntegerField(min_value=1, widget=UnfoldAdminIntegerFieldWidget)
    reason = forms.ChoiceField(
        choices=[c for c in StockMovement.REASON_CHOICES if c[0] in TRANSFER_REASONS],
        widget=UnfoldAdminSelectWidget,
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)

    def __init__(self, *args, stock, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = adjustment_services.ALLOWED_STOCK_TYPE_TRANSFERS.get(stock.stock_type, set())
        self.fields["to_type"].choices = [
            c for c in InventoryStock.STOCK_TYPE_CHOICES if c[0] in allowed
        ]


class StockAdjustmentForm(forms.Form):
    """Signed quantity change for adjust_inventory_stock()."""

    quantity_delta = forms.DecimalField(
        max_digits=12, decimal_places=2, label="Change in quantity",
        help_text="Signed: +2 for a stocktaking gain, -2 to write off.",
    )
    reason = forms.ChoiceField(choices=[("", "Choose reason")] + MANUAL_ADJUSTMENT_REASONS)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_quantity_delta(self):
        delta = self.cleaned_data["quantity_delta"]
        if delta == 0:
            raise forms.ValidationError("Enter a change other than zero.")
        return delta


class PartialLotAdjustmentForm(forms.Form):
    """Signed ml change for adjust_partial_lot()."""

    ml_delta = forms.DecimalField(
        max_digits=10, decimal_places=2, label="Change in ml",
        help_text="Signed: -5 for evaporation or spillage.",
    )
    reason = forms.ChoiceField(choices=[("", "Choose reason")] + MANUAL_ADJUSTMENT_REASONS)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_ml_delta(self):
        delta = self.cleaned_data["ml_delta"]
        if delta == 0:
            raise forms.ValidationError("Enter a change other than zero.")
        return delta


class ReorderLevelForm(forms.Form):
    """Reorder level for set_reorder_level(); blank clears it."""

    reorder_level = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, label="Reorder level",
        help_text="Leave blank for no reorder level.",
    )

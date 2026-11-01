"""Read-only inventory queries."""

from decimal import ROUND_FLOOR, Decimal

from django.db.models import Sum

from . import models as inv_models
from .services.reservation import decant_capacity_ml

ZERO = Decimal("0")


def in_store_available(variant_ids, warehouse):
    """{variant_id: whole units a counter sale can take now} in `warehouse`.

    Physical stock only (DEC-008): never booked incoming or PO quantities,
    whatever BOOKED_INCOMING_SALES_ENABLED says. A normal variant has its
    retail available; a decant variant has the whole decants its source can
    fill from open bottles plus sealed retail bottles, using the same
    capacity rule as the reservation. A display figure: the locked
    reservation at sale time stays authoritative. Three queries.
    """
    variant_ids = list(variant_ids)
    sources = {
        source.decant_variant_id: source
        for source in inv_models.DecantSource.objects.select_related("source_variant").filter(
            decant_variant_id__in=variant_ids
        )
    }
    stock_ids = set(variant_ids) - sources.keys() | {s.source_variant_id for s in sources.values()}
    retail = {
        stock.variant_id: max(stock.available, ZERO)
        for stock in inv_models.InventoryStock.objects.filter(
            variant_id__in=stock_ids, warehouse=warehouse,
            stock_type=inv_models.InventoryStock.STOCK_TYPE_RETAIL,
        )
    }
    lot_ml = {}
    if sources:
        lot_ml = dict(
            inv_models.PartialBottleLot.objects.filter(
                variant_id__in={s.source_variant_id for s in sources.values()},
                warehouse=warehouse, is_depleted=False,
            ).values("variant_id").annotate(ml=Sum("remaining_ml") - Sum("reserved_ml")).values_list("variant_id", "ml")
        )

    result = {}
    for variant_id in variant_ids:
        source = sources.get(variant_id)
        if source is None:
            units = retail.get(variant_id, ZERO)
        else:
            capacity = decant_capacity_ml(
                max(lot_ml.get(source.source_variant_id) or ZERO, ZERO),
                retail.get(source.source_variant_id, ZERO),
                source.source_variant.size_ml,
            )
            units = capacity / source.decant_volume_ml
        result[variant_id] = int(units.to_integral_value(rounding=ROUND_FLOOR))
    return result

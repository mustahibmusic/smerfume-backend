# DEC-013 data migration. An existing order gets one CPAY row only when it
# is unambiguously a completed, paid counter sale: in-store, delivered,
# paid by a counter method, total above zero, delivered time recorded and
# no payment row yet. The row takes the order's method, total and reference
# (a blank card/netbanking reference is accepted here only) and the local
# date of delivery. Numbers follow (delivered_at, id). Online, COD, prepaid
# and undelivered orders get no payment history.

from django.db import migrations
from django.utils import timezone

COUNTER_METHODS = ("cash", "upi", "card", "netbanking")


def backfill(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    OrderPayment = apps.get_model("orders", "OrderPayment")
    orders = (
        Order.objects.filter(
            channel="in_store", status="delivered", payment_method__in=COUNTER_METHODS,
            total__gt=0, delivered_at__isnull=False, payments__isnull=True,
        )
        .order_by("delivered_at", "id")
    )
    number = 0
    for order in orders.iterator():
        number += 1
        OrderPayment.objects.create(
            order_id=order.pk,
            payment_number=f"CPAY-{number:06d}",
            method=order.payment_method,
            amount=order.total,
            reference=(order.payment_reference or "").strip(),
            payment_date=timezone.localtime(order.delivered_at).date(),
            recorded_by_id=order.created_by_id,
        )
    with schema_editor.connection.cursor() as cursor:
        if number:
            cursor.execute("SELECT setval('orders_cpay_seq', %s, true)", [number])
        else:
            cursor.execute("SELECT setval('orders_cpay_seq', 1, false)")


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0013_order_payment"),
    ]

    operations = [
        # Reversing 0013 drops the table, so nothing to undo here.
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

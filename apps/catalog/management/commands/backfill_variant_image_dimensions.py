"""Fill width/height on gallery images created before the gallery manager.

Safe to rerun: only rows with missing dimensions are read. Files that are
missing or not readable images are reported and left unchanged. Run once
after deploying catalog.0009 (reads every such file from storage).
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from PIL import Image

from apps.catalog.models import ProductVariantImage


class Command(BaseCommand):
    help = "Fill missing width/height on ProductVariantImage rows from their files."

    def handle(self, *args, **options):
        updated = failed = 0
        pending = ProductVariantImage.objects.filter(Q(width__isnull=True) | Q(height__isnull=True))
        for image in pending.iterator():
            try:
                with image.image.open("rb") as handle, Image.open(handle) as decoded:
                    width, height = decoded.size
            except Exception as exc:  # noqa: BLE001 - any unreadable file is reported, not fatal
                failed += 1
                self.stderr.write(f"Image {image.public_id} ({image.image.name}): {type(exc).__name__}")
                continue
            ProductVariantImage.objects.filter(pk=image.pk).update(width=width, height=height)
            updated += 1
        self.stdout.write(f"{updated} updated, {failed} failed.")

"""Variant gallery image services: the only write path for gallery images.

Images are processed (validated, normalised) before any lock is taken.
Then every function locks the ProductVariant row first, then its image
rows, so concurrent changes to one gallery are serialised. Role changes
demote the current holder before promoting, so the one-primary /
one-secondary constraints hold at every statement; removing an image never
promotes another one. Old files are deleted only after the transaction
commits; a newly written file is deleted again if the transaction fails.
"""

import uuid
from dataclasses import dataclass, field

from django.contrib.admin.models import CHANGE, LogEntry
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Case, IntegerField, Max, Value, When
from django.utils import timezone

from .image_processing import ImageRejected, process_image
from .models import ProductVariant, ProductVariantImage

MAX_FILES_PER_UPLOAD = ProductVariantImage.MAX_IMAGES_PER_VARIANT
ALT_TEXT_MAX_LENGTH = 255
ROLES = dict(ProductVariantImage.ROLE_CHOICES)


class VariantImageError(Exception):
    """A gallery change is not allowed (message is user-facing)."""


@dataclass
class UploadResult:
    created: list = field(default_factory=list)
    rejected: list = field(default_factory=list)  # (file name, message)
    warnings: list = field(default_factory=list)  # (file name, message)


def display_ordered_images(queryset):
    """Primary first, then secondary, then the rest by sort_order, id."""
    rank = Case(
        When(role=ProductVariantImage.ROLE_PRIMARY, then=Value(0)),
        When(role=ProductVariantImage.ROLE_SECONDARY, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    return queryset.order_by(rank, "sort_order", "id")


def _lock_variant(variant):
    return ProductVariant.objects.select_for_update().get(pk=variant.pk)


def _lock_image(variant, image):
    return ProductVariantImage.objects.select_for_update().get(pk=image.pk, variant=variant)


def _log(variant, user, message):
    LogEntry.objects.log_actions(user.pk, [variant], CHANGE, change_message=message, single_object=True)


def _storage():
    return ProductVariantImage._meta.get_field("image").storage


def _delete_after_commit(name):
    if name:
        storage = _storage()
        transaction.on_commit(lambda: storage.delete(name))


def _write_file(image, processed):
    """Save processed bytes under a new storage name (see upload_to)."""
    image.image.save(f"upload.{processed.extension}", ContentFile(processed.content), save=False)
    image.width = processed.width
    image.height = processed.height
    image.content_hash = processed.sha256
    return image.image.name


def add_variant_images(variant, files, user):
    """Add uploaded files to the gallery as gallery-role images, appended
    after the existing ones. Each file succeeds or is rejected on its own."""
    files = list(files)
    if not files:
        raise VariantImageError("Choose at least one image.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise VariantImageError(f"Upload at most {MAX_FILES_PER_UPLOAD} images at a time.")

    result = UploadResult()
    processed = []
    for upload in files:
        name = getattr(upload, "name", "image")
        try:
            image = process_image(upload)
        except ImageRejected as exc:
            result.rejected.append((name, str(exc)))
            continue
        processed.append((name, image))

    written = []
    try:
        with transaction.atomic():
            variant = _lock_variant(variant)
            existing = list(variant.images.select_for_update())
            count = len(existing)
            hashes = {image.content_hash for image in existing if image.content_hash}
            next_order = (variant.images.aggregate(top=Max("sort_order"))["top"] + 1) if existing else 0
            for name, image in processed:
                if count >= ProductVariantImage.MAX_IMAGES_PER_VARIANT:
                    result.rejected.append(
                        (name, f"This variant already has {ProductVariantImage.MAX_IMAGES_PER_VARIANT} images.")
                    )
                    continue
                if image.sha256 in hashes:
                    result.rejected.append((name, "This image is already in the gallery."))
                    continue
                row = ProductVariantImage(
                    variant=variant, role=ProductVariantImage.ROLE_GALLERY, sort_order=next_order,
                )
                written.append(_write_file(row, image))
                row.save()
                result.created.append(row)
                result.warnings.extend((name, note) for note in image.warnings)
                hashes.add(image.sha256)
                count += 1
                next_order += 1
            if result.created:
                _log(variant, user, f"Added {len(result.created)} gallery image(s).")
    except Exception:
        for name in written:
            _storage().delete(name)
        raise
    return result


def replace_variant_image(image, upload, user):
    """Replace an image's file with an edited version (new storage object;
    the old file is deleted after commit). Role, alt text and order stay."""
    try:
        processed = process_image(upload)
    except ImageRejected as exc:
        raise VariantImageError(str(exc)) from exc

    new_name = None
    try:
        with transaction.atomic():
            variant = _lock_variant(image.variant)
            image = _lock_image(variant, image)
            if processed.sha256 == image.content_hash:
                return image
            if variant.images.exclude(pk=image.pk).filter(content_hash=processed.sha256).exists():
                raise VariantImageError("This image is already in the gallery.")
            old_name = image.image.name
            new_name = _write_file(image, processed)
            image.save(update_fields=["image", "width", "height", "content_hash", "updated_at"])
            _delete_after_commit(old_name)
            _log(variant, user, f"Edited gallery image {image.public_id}.")
    except Exception:
        if new_name:
            _storage().delete(new_name)
        raise
    return image


@transaction.atomic
def set_variant_image_role(image, role, user):
    """Make an image primary, secondary or a plain gallery image. The current
    primary/secondary holder is demoted to gallery first."""
    if role not in ROLES:
        raise VariantImageError("Choose primary, secondary or gallery.")
    variant = _lock_variant(image.variant)
    image = _lock_image(variant, image)
    if image.role == role:
        return image
    if role != ProductVariantImage.ROLE_GALLERY:
        variant.images.filter(role=role).exclude(pk=image.pk).update(
            role=ProductVariantImage.ROLE_GALLERY, updated_at=timezone.now(),
        )
    image.role = role
    image.save(update_fields=["role", "updated_at"])
    _log(variant, user, f"Set gallery image {image.public_id} to {ROLES[role]}.")
    return image


@transaction.atomic
def reorder_variant_images(variant, ordered_public_ids, user):
    """Set the gallery order. The list must name every image of the variant
    exactly once; sort_order becomes 0..n-1 in that order."""
    variant = _lock_variant(variant)
    images = {str(image.public_id): image for image in variant.images.select_for_update()}
    try:
        ordered = [str(uuid.UUID(str(public_id))) for public_id in ordered_public_ids]
    except ValueError as exc:
        raise VariantImageError("The order must list every image of this variant exactly once.") from exc
    if len(ordered) != len(images) or set(ordered) != set(images):
        raise VariantImageError("The order must list every image of this variant exactly once.")
    changed = []
    for position, public_id in enumerate(ordered):
        image = images[public_id]
        if image.sort_order != position:
            image.sort_order = position
            changed.append(image)
    if changed:
        ProductVariantImage.objects.bulk_update(changed, ["sort_order"])
        _log(variant, user, "Reordered gallery images.")


@transaction.atomic
def update_variant_image_alt_text(image, alt_text, user):
    alt_text = (alt_text or "").strip()
    if len(alt_text) > ALT_TEXT_MAX_LENGTH:
        raise VariantImageError(f"Alt text can be at most {ALT_TEXT_MAX_LENGTH} characters.")
    variant = _lock_variant(image.variant)
    image = _lock_image(variant, image)
    if image.alt_text != alt_text:
        image.alt_text = alt_text
        image.save(update_fields=["alt_text", "updated_at"])
        _log(variant, user, f"Changed alt text of gallery image {image.public_id}.")
    return image


@transaction.atomic
def remove_variant_image(image, user):
    """Delete an image (file removed after commit). No other image is
    promoted into its role."""
    variant = _lock_variant(image.variant)
    image = _lock_image(variant, image)
    name, public_id, role = image.image.name, image.public_id, image.get_role_display()
    image.delete()
    _delete_after_commit(name)
    _log(variant, user, f"Removed {role.lower()} gallery image {public_id}.")

"""DEC-011 supplier payment proof: upload validation.

The browser-supplied content type and extension are never trusted. The size
is checked before anything is read, then the type is detected from the file
content (PDF header, or Pillow for JPEG/PNG/WebP). Stored names are random;
the original name is kept only as sanitized display text.
"""

import os
import uuid

from django.utils.text import get_valid_filename
from PIL import Image, UnidentifiedImageError

from .models import SupplierPaymentAttachment as Attachment

_PILLOW_TYPES = {"JPEG": Attachment.TYPE_JPEG, "PNG": Attachment.TYPE_PNG, "WEBP": Attachment.TYPE_WEBP}
_EXTENSIONS = {
    Attachment.TYPE_PDF: ".pdf", Attachment.TYPE_JPEG: ".jpg",
    Attachment.TYPE_PNG: ".png", Attachment.TYPE_WEBP: ".webp",
}


class AttachmentError(Exception):
    """Raised when an uploaded proof file is not acceptable."""


def _detect_content_type(upload):
    upload.seek(0)
    if upload.read(5) == b"%PDF-":
        upload.seek(0)
        return Attachment.TYPE_PDF
    upload.seek(0)
    try:
        with Image.open(upload) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        return None
    finally:
        upload.seek(0)
    return _PILLOW_TYPES.get(image_format)


def display_name(raw_name):
    base = os.path.basename((raw_name or "").replace("\\", "/"))
    try:
        cleaned = get_valid_filename(base)
    except Exception:  # SuspiciousFileOperation for names like "." or ".."
        cleaned = ""
    return (cleaned or "attachment")[:255]


def validate_upload(upload):
    """Return (content_type, original_name, storage_name) or raise AttachmentError."""
    name = display_name(getattr(upload, "name", ""))
    size = upload.size or 0
    if size <= 0:
        raise AttachmentError(f"{name} is empty.")
    if size > Attachment.MAX_SIZE:
        raise AttachmentError(f"{name} is larger than 5 MB.")
    content_type = _detect_content_type(upload)
    if content_type is None:
        raise AttachmentError(f"{name} is not a PDF, JPEG, PNG or WebP file.")
    return content_type, name, f"{uuid.uuid4().hex}{_EXTENSIONS[content_type]}"

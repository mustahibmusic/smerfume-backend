"""Validation and normalisation of variant gallery images.

The server is authoritative: browser-side checks and editing are a
convenience only. Every image is decoded, checked, normalised (EXIF
rotation applied, metadata removed, colour mode fixed), resized to at most
MAX_OUTPUT_SIDE and re-encoded, so stored files are predictable and carry
no camera/GPS data. See docs/superpowers/specs/2026-10-31-variant-image-gallery-design.md.
"""

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError, features

ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}  # MPO: multi-picture JPEG (phones)
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_INPUT_SIDE = 6000
MAX_INPUT_PIXELS = 36_000_000
MIN_SHORTEST_SIDE = 600
RECOMMENDED_SHORTEST_SIDE = 1200
MAX_OUTPUT_SIDE = 2000
QUALITY = 90


class ImageRejected(Exception):
    """The upload is not an acceptable product image (message is user-facing)."""


@dataclass(frozen=True)
class ProcessedImage:
    content: bytes
    extension: str
    width: int
    height: int
    sha256: str
    warnings: tuple


def process_image(uploaded_file):
    """Validate and normalise an uploaded image. Raises ImageRejected."""
    if getattr(uploaded_file, "size", 0) > MAX_UPLOAD_BYTES:
        raise ImageRejected("The file is larger than 15 MB.")
    uploaded_file.seek(0)
    data = uploaded_file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected("The file is larger than 15 MB.")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = _open_checked(data)
            return _normalise(image)
        except ImageRejected:
            raise
        except (
            UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError,
            Image.DecompressionBombError, Image.DecompressionBombWarning,
        ) as exc:
            raise ImageRejected("The file is not a valid JPEG, PNG or WebP image.") from exc


def _open_checked(data):
    image = Image.open(io.BytesIO(data))
    if image.format not in ALLOWED_FORMATS:
        raise ImageRejected("Only JPEG, PNG and WebP images are accepted.")
    if image.format != "MPO" and getattr(image, "is_animated", False):
        raise ImageRejected("Animated images are not accepted.")
    width, height = image.size
    if width > MAX_INPUT_SIDE or height > MAX_INPUT_SIDE or width * height > MAX_INPUT_PIXELS:
        raise ImageRejected(
            f"The image is {width}x{height}; at most {MAX_INPUT_SIDE}x{MAX_INPUT_SIDE} px is accepted."
        )
    image.verify()
    # verify() leaves the image unusable; reopen and fully decode.
    image = Image.open(io.BytesIO(data))
    image.seek(0)
    image.load()
    return image


def _normalise(image):
    original_mode = image.mode
    icc_profile = image.info.get("icc_profile")
    image = ImageOps.exif_transpose(image)

    width, height = image.size
    shortest = min(width, height)
    if shortest < MIN_SHORTEST_SIDE:
        raise ImageRejected(
            f"The image is {width}x{height}; the shortest side must be at least {MIN_SHORTEST_SIDE} px."
        )
    notes = ()
    if shortest < RECOMMENDED_SHORTEST_SIDE:
        notes = (
            f"Low resolution ({width}x{height}); at least {RECOMMENDED_SHORTEST_SIDE} px on the "
            "shortest side is recommended for the storefront.",
        )

    has_alpha = _has_transparency(image)
    image = image.convert("RGBA" if has_alpha else "RGB")
    if max(image.size) > MAX_OUTPUT_SIDE:
        image.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)

    fmt, extension, params = _output_format(has_alpha)
    if icc_profile and original_mode != "CMYK":
        # Colour profile only; a CMYK profile would be wrong for RGB output.
        params["icc_profile"] = icc_profile
    buffer = io.BytesIO()
    image.save(buffer, fmt, **params)  # no exif/xmp passed: metadata removed
    content = buffer.getvalue()
    if len(content) > MAX_OUTPUT_BYTES:
        raise ImageRejected("The processed image is larger than 5 MB.")
    return ProcessedImage(
        content=content, extension=extension, width=image.width, height=image.height,
        sha256=hashlib.sha256(content).hexdigest(), warnings=notes,
    )


def _has_transparency(image):
    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if image.mode not in ("RGBA", "LA", "PA"):
        return False
    return image.getchannel("A").getextrema()[0] < 255


def _output_format(has_alpha):
    if features.check("webp"):
        return "WEBP", "webp", {"quality": QUALITY}
    if has_alpha:
        return "PNG", "png", {"optimize": True}
    return "JPEG", "jpg", {"quality": QUALITY}

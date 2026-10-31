# Variant Image Gallery — Design

Date: 2026-10-31. Status: approved, in implementation on
`feat/catalog-variant-image-gallery`.

Internal admin feature for managing many images per `ProductVariant` (SKU).
Builds on the existing `ProductVariantImage` model (roles primary /
secondary / gallery, one primary and one secondary per variant enforced by
conditional unique constraints, no auto-promotion on delete).

## Scope

- Dedicated **Manage Images** admin page per variant: thumbnail grid, image
  count, multi-file upload, role badges, Make primary / Make secondary /
  Gallery, Edit, Remove (confirmation page), drag-and-drop reorder with
  keyboard controls, alt text editing.
- Client-side **editor** (Cropper.js 1.7.0, vendored with its MIT licence):
  crop, zoom, rotate, flip, free and 1:1 aspect, brightness, contrast
  (canvas filters, hidden when unsupported), preview, reset. No AI editing,
  background removal, sharpening, filters or curves.
- Variant change page: compact read-only preview with a Manage Images
  button. The old inline is removed so there is one write path.
- Legacy `ProductVariant.image`: removed from the variant form; an existing
  legacy image is shown read-only as "Legacy image (not used by the
  gallery)". The DB column and the API `image` field stay unchanged.
  Migrating legacy images into the gallery is a separate later decision.

## Limits and validation (server is authoritative)

- At most **20 images** per variant (`MAX_IMAGES_PER_VARIANT`).
- Per upload request: at most 20 files and no more than the free slots;
  each file at most 15 MB.
- Accepted: JPEG (MPO first frame), PNG, WebP, detected from content.
  Rejected: SVG, GIF, HEIC, animated images, anything else.
- Malformed: open, verify, reopen and load; any error rejects.
- Input safety: header size checked before decoding; each side at most
  6000 px and at most 36 MP; Pillow decompression-bomb warnings are errors.
- Shortest side at least **600 px** (after EXIF rotation); below 1200 px
  saves with a low-resolution warning.
- Normalisation: EXIF orientation applied; EXIF/XMP/GPS removed (ICC colour
  profile kept); CMYK and other modes converted to RGB, or RGBA when
  transparent.
- Resize to at most **2000 px** on the longest side (never upscale), for
  every upload, edited or not.
- Output: **WebP** quality 90; if Pillow lacks WebP, PNG for transparent
  images and JPEG quality 90 otherwise. Final file at most 5 MB.
- Duplicates: SHA-256 of the final bytes, unique per variant.
- Alt text: stripped, at most 255 characters.

## Data model

Migration `catalog.0009_variant_image_metadata`:
- `width`, `height`: nullable positive integers set by the services (not
  `ImageField.width_field`, which would open the file on every load of a
  row with empty dimensions).
- `content_hash`: 64 characters, default `""`.
- `UniqueConstraint(variant, content_hash)` where `content_hash != ""`.
- New uploads stored as
  `catalog/variant_images/<variant public_id>/<uuid>.<ext>`.

Existing rows keep their files; `backfill_variant_image_dimensions` fills
dimensions (rerunnable, deploy step). No new permissions.

## Services

`apps/catalog/image_processing.py`: `process_image(file) -> ProcessedImage`
or `ImageRejected`.

`apps/catalog/image_services.py`: `add_variant_images`,
`replace_variant_image`, `set_variant_image_role`,
`reorder_variant_images`, `update_variant_image_alt_text`,
`remove_variant_image`. Images are processed before locking; then the
variant row is locked, then image rows. Role changes demote the current
holder first, then promote. No auto-promotion. Old files are deleted only
after commit; a new file is deleted if the transaction fails. Each change
writes an admin `LogEntry` on the variant.

## Permissions

Existing catalog permissions: view (variant + image) for the page;
`add_productvariantimage` upload; `change_productvariantimage` edit, role,
reorder, alt text; `delete_productvariantimage` remove. With the current
staff groups only Manager can change images; Ops and Warehouse can view.

## API

Additive only: `secondary_image` next to `primary_image`, and `width` /
`height` on each image (null for legacy rows until backfilled). The order
of `images` stays `sort_order, id`. Reordering `images` to primary,
secondary, then gallery is **gated** until the storefront repo is checked.

## Originals

No second original copy is kept. Replacing an image writes a new storage
object; the old one is deleted after commit.

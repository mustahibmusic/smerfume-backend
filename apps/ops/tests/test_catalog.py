"""Catalog pages on the Smerfume Panel: lists, hierarchy detail pages,
permissions, quick view and the variant gallery through image_services."""

import io
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.catalog.models import (
    Brand,
    Category,
    EditionNote,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
    ProductVariantImage,
)
from apps.ops.tests.test_purchases import User, _user

PASSWORD = "Str0ng-Passw0rd!"
LISTS = ("ops:product_list", "ops:edition_list", "ops:variant_list", "ops:brand_list", "ops:note_list")


def _image(color=(200, 30, 30), name="photo.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (800, 800), color).save(buffer, "JPEG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


class _Catalog(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(username="catroot", email="catroot@example.com", password=PASSWORD)
        self.brand = Brand.objects.create(name="Ops Brand", slug="ops-brand", brand_category="niche")
        category = Category.objects.create(name="Ops Cat", slug="ops-cat")
        self.product = Product.objects.create(name="Oud One", slug="oud-one", brand=self.brand, category=category)
        self.edition = ProductEdition.objects.create(product=self.product, name="Intense", slug="intense")
        self.note = PerfumeNote.objects.create(name="Saffron", notes_category="spicy")
        EditionNote.objects.create(edition=self.edition, note=self.note, position="top")
        self.variant = ProductVariant.objects.create(
            edition=self.edition, sku="OPS-CAT-100", size_ml=100, mrp=Decimal("5000"), selling_price=Decimal("4500"),
        )


class CatalogPageTests(_Catalog):
    def test_lists_render_with_list_pattern_and_quick_view(self):
        self.client.force_login(self.owner)
        for name in LISTS:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            html = response.content.decode()
            for marker in ("data-select-all", "data-bulk-bar", "data-quick-drawer", 'class="row-menu"'):
                self.assertIn(marker, html, name)
            for obj in response.context["object_list"]:
                self.assertEqual(obj.ops_actions[0].kind, "quick", name)
                self.assertFalse({a.kind for a in obj.ops_actions} & {"edit", "delete"}, name)
            self.assertEqual(response.context["ops_bulk_actions"], [], name)

    def test_hierarchy_is_linked_with_public_identifiers(self):
        self.client.force_login(self.owner)
        product = self.client.get(reverse("ops:product_detail", args=["oud-one"])).content.decode()
        self.assertIn(reverse("ops:edition_detail", args=[self.edition.public_id]), product)
        self.assertIn(reverse("ops:variant_detail", args=["OPS-CAT-100"]), product)
        self.assertIn(reverse("ops:brand_detail", args=["ops-brand"]), product)
        edition = self.client.get(reverse("ops:edition_detail", args=[self.edition.public_id])).content.decode()
        self.assertIn("Saffron", edition)
        self.assertIn(reverse("ops:product_detail", args=["oud-one"]), edition)
        variant = self.client.get(reverse("ops:variant_detail", args=["OPS-CAT-100"])).content.decode()
        self.assertIn("4500", variant)
        self.assertIn(reverse("ops:edition_detail", args=[self.edition.public_id]), variant)
        note = self.client.get(reverse("ops:note_detail", args=[self.note.public_id])).content.decode()
        self.assertIn("Top", note)
        brand = self.client.get(reverse("ops:brand_detail", args=["ops-brand"])).content.decode()
        self.assertIn("Oud One", brand)

    def test_filters(self):
        self.client.force_login(self.owner)
        ProductVariant.objects.create(
            edition=self.edition, sku="OPS-CAT-DEC", size_ml=5, is_decant=True,
            mrp=Decimal("400"), selling_price=Decimal("350"),
        )
        page = self.client.get(reverse("ops:variant_list"), {"kind": "decant"})
        self.assertEqual([v.sku for v in page.context["object_list"]], ["OPS-CAT-DEC"])
        page = self.client.get(reverse("ops:product_list"), {"q": "zzz"})
        self.assertEqual(list(page.context["object_list"]), [])
        page = self.client.get(reverse("ops:brand_list"), {"brand_category": "bogus"})
        self.assertEqual(page.status_code, 200)  # invalid value reported and ignored

    def test_unknown_identifiers_are_branded_404(self):
        self.client.force_login(self.owner)
        for url in (reverse("ops:variant_detail", args=["NOPE"]), reverse("ops:product_detail", args=["nope"])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertTemplateUsed(response, "ops/404.html")

    def test_each_page_needs_its_view_permission(self):
        self.client.force_login(_user("catvariants", "catalog.view_productvariant"))
        self.assertEqual(self.client.get(reverse("ops:variant_list")).status_code, 200)
        for name in ("ops:product_list", "ops:edition_list", "ops:brand_list", "ops:note_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        # Without image view permission the gallery is not shown.
        html = self.client.get(reverse("ops:variant_detail", args=["OPS-CAT-100"])).content.decode()
        self.assertNotIn('id="gallery"', html)
        # Links to levels the user cannot open are plain text.
        self.assertNotIn(reverse("ops:edition_detail", args=[self.edition.public_id]), html)

    def test_quick_view_renders_without_shell(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("ops:variant_detail", args=["OPS-CAT-100"]), {"quick": "1"})
        self.assertTemplateUsed(response, "ops/quick.html")
        self.assertNotIn('id="ops-sidebar"', response.content.decode())


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class GalleryTests(_Catalog):
    def _upload(self, *files):
        return self.client.post(
            reverse("ops:variant_image_upload", args=["OPS-CAT-100"]), {"images": list(files)},
        )

    def _act(self, image, action, **data):
        return self.client.post(reverse("ops:variant_image_action", args=["OPS-CAT-100", image.public_id, action]), data)

    def test_upload_role_alt_text_reorder_remove_through_services(self):
        self.client.force_login(self.owner)
        response = self._upload(_image((200, 0, 0), "a.jpg"), _image((0, 200, 0), "b.jpg"), _image((0, 0, 200), "c.jpg"))
        self.assertRedirects(response, reverse("ops:variant_detail", args=["OPS-CAT-100"]) + "#gallery",
                             fetch_redirect_response=False)
        first, second, third = self.variant.images.order_by("sort_order")
        self.assertEqual({first.role, second.role}, {"gallery"})

        self._act(first, "role", **{f"{first.public_id}-role": "primary"})
        first.refresh_from_db()
        self.assertEqual(first.role, "primary")

        self._act(second, "alt-text", **{f"{second.public_id}-alt_text": "Bottle front"})
        second.refresh_from_db()
        self.assertEqual(second.alt_text, "Bottle front")

        self._act(third, "up")
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertLess(third.sort_order, second.sort_order)

        self._act(first, "remove")
        self.assertFalse(ProductVariantImage.objects.filter(pk=first.pk).exists())
        # Removing the primary never promotes another image.
        self.assertFalse(self.variant.images.filter(role="primary").exists())

    def test_duplicate_upload_is_rejected_by_service(self):
        self.client.force_login(self.owner)
        self._upload(_image())
        response = self._upload(_image())
        self.assertEqual(self.variant.images.count(), 1)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("already in the gallery" in m for m in messages))

    def test_gallery_writes_need_their_own_permission(self):
        self.client.force_login(self.owner)
        self._upload(_image())
        image = self.variant.images.get()
        viewer = _user("galleryview", "catalog.view_productvariant", "catalog.view_productvariantimage")
        self.client.force_login(viewer)
        html = self.client.get(reverse("ops:variant_detail", args=["OPS-CAT-100"])).content.decode()
        self.assertIn('id="gallery"', html)
        self.assertNotIn("/remove/", html)
        self.assertNotIn("/role/", html)
        self.assertNotIn('enctype="multipart/form-data"', html)
        self.assertEqual(self._upload(_image((1, 2, 3))).status_code, 403)
        self.assertEqual(self._act(image, "remove").status_code, 403)
        self.assertEqual(self._act(image, "role", **{f"{image.public_id}-role": "primary"}).status_code, 403)
        self.assertEqual(self._act(image, "bogus").status_code, 403)
        self.assertEqual(self.variant.images.count(), 1)
        self.assertEqual(self.client.get(
            reverse("ops:variant_image_action", args=["OPS-CAT-100", image.public_id, "remove"])
        ).status_code, 403)

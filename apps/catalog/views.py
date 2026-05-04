from rest_framework import filters, viewsets
from rest_framework.permissions import AllowAny

from .models import Brand, Category, PerfumeNote, Product
from .serializers import (
    BrandSerializer,
    CategorySerializer,
    PerfumeNoteSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
)


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public product catalog.

    List  — GET /api/catalog/products/
    Detail — GET /api/catalog/products/{slug}/

    Query params:
      ?search=        — name, brand name, edition name
      ?brand=<slug>
      ?category=<slug>
      ?gender=men|women|unisex
      ?concentration=edc|edt|edp|extrait
      ?note=<note-name>  (comma-separated, AND logic)
      ?is_best_seller=true
      ?is_new_arrival=true
    """

    permission_classes = [AllowAny]
    lookup_field = "slug"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "brand__name", "editions__name"]
    ordering_fields = ["name", "brand__name"]
    ordering = ["name"]

    def get_queryset(self):
        qs = (
            Product.objects.filter(is_active=True)
            .select_related("brand", "category")
            .prefetch_related(
                "editions",
                "editions__variants",
                "editions__edition_notes__note",
            )
        )

        params = self.request.query_params

        brand_slug = params.get("brand")
        if brand_slug:
            qs = qs.filter(brand__slug=brand_slug)

        category_slug = params.get("category")
        if category_slug:
            qs = qs.filter(category__slug=category_slug)

        gender = params.get("gender")
        if gender:
            qs = qs.filter(editions__gender=gender)

        concentration = params.get("concentration")
        if concentration:
            qs = qs.filter(editions__concentration=concentration)

        note = params.get("note")
        if note:
            for note_name in note.split(","):
                qs = qs.filter(editions__edition_notes__note__name__iexact=note_name.strip())

        if params.get("is_best_seller", "").lower() == "true":
            qs = qs.filter(editions__is_best_seller=True)

        if params.get("is_new_arrival", "").lower() == "true":
            qs = qs.filter(editions__is_new_arrival=True)

        return qs.distinct()

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ProductDetailSerializer
        return ProductListSerializer


class BrandViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    queryset = Brand.objects.filter(is_active=True).order_by("name")
    serializer_class = BrandSerializer
    lookup_field = "slug"
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    queryset = Category.objects.filter(is_active=True).order_by("name")
    serializer_class = CategorySerializer
    lookup_field = "slug"


class PerfumeNoteViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    queryset = PerfumeNote.objects.filter(is_active=True).order_by("name")
    serializer_class = PerfumeNoteSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "notes_category"]

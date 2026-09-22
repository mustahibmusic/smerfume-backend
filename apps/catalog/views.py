from apps.inventory.models import InventoryStock
from django.db.models import Prefetch
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import filters, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny

from .models import (
    Brand,
    Category,
    EditionNote,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
    ProductVariantImage,
)
from .serializers import (
    BrandSerializer,
    CategorySerializer,
    PerfumeNoteSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
)

# Precomputed valid choice sets — avoids recomputing per request
_VALID_GENDERS = {c[0] for c in ProductEdition.GENDER_CHOICES}
_VALID_CONCENTRATIONS = {c[0] for c in ProductEdition.CONCENTRATION_CHOICES}


def _slug_path_param(resource):
    return OpenApiParameter(
        "slug",
        str,
        OpenApiParameter.PATH,
        description=f"URL slug of the {resource}.",
    )


def _not_found_response(resource, model_name):
    return OpenApiResponse(
        description=f"No active {resource} exists with this identifier.",
        examples=[
            OpenApiExample(
                "Not found",
                value={"detail": f"No {model_name} matches the given query."},
            )
        ],
    )


@extend_schema_view(
    list=extend_schema(
        tags=["Catalog"],
        summary="List products",
        description=(
            "Return all active products with their editions and variants.\n\n"
            "**Filters:**\n"
            "- `search` — name, brand name, or edition name (min 2 chars)\n"
            "- `brand` — brand slug\n"
            "- `category` — category slug\n"
            "- `gender` — `men` | `women` | `unisex`\n"
            "- `concentration` — `edc` | `edt` | `edp` | `extrait` | `attar`\n"
            "- `note` — comma-separated note names, AND logic, max 5\n"
            "- `is_best_seller` — `true` | `false`\n"
            "- `is_new_arrival` — `true` | `false`\n"
            "- `ordering` — `name` | `-name` | `brand__name` | `-brand__name`"
        ),
        parameters=[
            OpenApiParameter("search", str, description="Search by name, brand, or edition (min 2 chars)"),
            OpenApiParameter("brand", str, description="Filter by brand slug"),
            OpenApiParameter("category", str, description="Filter by category slug"),
            OpenApiParameter("gender", str, description="men | women | unisex"),
            OpenApiParameter("concentration", str, description="edc | edt | edp | extrait | attar"),
            OpenApiParameter("note", str, description="Comma-separated note names (AND logic, max 5)"),
            OpenApiParameter("is_best_seller", str, description="true | false"),
            OpenApiParameter("is_new_arrival", str, description="true | false"),
            OpenApiParameter("ordering", str, description="name | -name | brand__name | -brand__name"),
        ],
        responses={
            200: ProductListSerializer(many=True),
            400: OpenApiResponse(
                description=(
                    "Invalid filter value: unknown `gender` or `concentration`, "
                    "`is_best_seller` / `is_new_arrival` not `true` or `false`, "
                    "or more than 5 `note` values."
                ),
                examples=[
                    OpenApiExample(
                        "Invalid gender",
                        value={"gender": "Invalid value. Must be one of: men, unisex, women."},
                    ),
                    OpenApiExample(
                        "Invalid boolean flag",
                        value={"is_best_seller": "Must be 'true' or 'false'."},
                    ),
                    OpenApiExample(
                        "Too many notes",
                        value={"note": "Maximum 5 notes can be specified."},
                    ),
                ],
            ),
        },
        auth=[],
    ),
    retrieve=extend_schema(
        tags=["Catalog"],
        summary="Get product detail",
        description="Return full detail for a single product including all editions, notes, and variants.",
        parameters=[_slug_path_param("product")],
        responses={
            200: ProductDetailSerializer,
            404: _not_found_response("product", "Product"),
        },
        auth=[],
    ),
)
class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    lookup_field = "slug"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "brand__name", "editions__name"]
    ordering_fields = ["name", "brand__name"]
    ordering = ["name"]

    def get_queryset(self):
        # self.request is a DRF Request, but the underlying Django HttpRequest
        # exposes GET instead of query_params. Using GET keeps the code type-safe
        # for static analyzers without changing runtime behavior.
        params = self.request.GET

        qs = (
            Product.objects.filter(is_active=True)
            .select_related("brand", "category")
            .prefetch_related(
                # Explicit Prefetch objects so inactive editions/variants are
                # excluded from serialized output.
                Prefetch(
                    "editions",
                    queryset=ProductEdition.objects.filter(is_active=True),
                ),
                Prefetch(
                    "editions__variants",
                    queryset=ProductVariant.objects.filter(is_active=True),
                ),
                # select_related("note") here avoids a per-EditionNote DB hit
                # in EditionNotesGroupedSerializer, which now reads from cache.
                Prefetch(
                    "editions__edition_notes",
                    queryset=EditionNote.objects.select_related("note"),
                ),
                # Gallery images for ProductVariantSerializer.images/primary_image
                Prefetch(
                    "editions__variants__images",
                    queryset=ProductVariantImage.objects.all(),
                ),
                # Retail stock only, for ProductVariantSerializer.is_available —
                # matches what apps.inventory.services.reservation actually
                # reserves against.
                Prefetch(
                    "editions__variants__inventory_stocks",
                    queryset=InventoryStock.objects.filter(
                        stock_type=InventoryStock.STOCK_TYPE_RETAIL
                    ),
                ),
            )
        )

        # ── Product-level filters ──────────────────────────────────────────────

        brand_slug = params.get("brand", "").strip()
        if brand_slug:
            qs = qs.filter(brand__slug=brand_slug)

        category_slug = params.get("category", "").strip()
        if category_slug:
            qs = qs.filter(category__slug=category_slug)

        # ── Edition-level filters ──────────────────────────────────────────────
        # All edition constraints are collected and applied as a single subquery
        # on ProductEdition. This prevents cross-edition contamination: without
        # the subquery, ?gender=men&concentration=edp would match a product
        # whose Edition A is (men/edt) and Edition B is (women/edp) — two
        # different editions satisfying two different constraints.
        edition_filters = {}

        gender = params.get("gender", "").strip()
        if gender:
            if gender not in _VALID_GENDERS:
                raise ValidationError(
                    {"gender": f"Invalid value. Must be one of: {', '.join(sorted(_VALID_GENDERS))}."}
                )
            edition_filters["gender"] = gender

        concentration = params.get("concentration", "").strip()
        if concentration:
            if concentration not in _VALID_CONCENTRATIONS:
                raise ValidationError(
                    {"concentration": f"Invalid value. Must be one of: {', '.join(sorted(_VALID_CONCENTRATIONS))}."}
                )
            edition_filters["concentration"] = concentration

        is_best_seller = params.get("is_best_seller", "").strip().lower()
        if is_best_seller:
            if is_best_seller not in ("true", "false"):
                raise ValidationError({"is_best_seller": "Must be 'true' or 'false'."})
            if is_best_seller == "true":
                edition_filters["is_best_seller"] = True

        is_new_arrival = params.get("is_new_arrival", "").strip().lower()
        if is_new_arrival:
            if is_new_arrival not in ("true", "false"):
                raise ValidationError({"is_new_arrival": "Must be 'true' or 'false'."})
            if is_new_arrival == "true":
                edition_filters["is_new_arrival"] = True

        note_names = []
        note = params.get("note", "").strip()
        if note:
            note_names = [n.strip() for n in note.split(",") if n.strip()]
            if len(note_names) > 5:
                raise ValidationError({"note": "Maximum 5 notes can be specified."})

        if edition_filters or note_names:
            editions_qs = ProductEdition.objects.filter(is_active=True, **edition_filters)
            # Each chained filter for notes creates a separate JOIN on
            # edition_notes, correctly enforcing AND logic on the same edition.
            for note_name in note_names:
                editions_qs = editions_qs.filter(
                    edition_notes__note__name__iexact=note_name
                )
            qs = qs.filter(pk__in=editions_qs.values("product_id"))

        # distinct() guards against duplicates from SearchFilter's editions__name
        # JOIN and from any remaining multi-valued traversals.
        return qs.distinct()

    def filter_queryset(self, queryset):
        # Reject single-character search terms: they are too broad and trigger
        # expensive icontains JOINs across three tables.
        search = self.request.query_params.get(
            filters.SearchFilter.search_param, ""
        ).strip()
        if search and len(search) < 2:
            return queryset.none()
        return super().filter_queryset(queryset)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ProductDetailSerializer
        return ProductListSerializer


@extend_schema_view(
    list=extend_schema(
        tags=["Catalog"],
        summary="List brands",
        description="Return all active brands. Supports `?search=` by name.",
        auth=[],
    ),
    retrieve=extend_schema(
        tags=["Catalog"],
        summary="Get brand detail",
        description="Return a single active brand by slug.",
        parameters=[_slug_path_param("brand")],
        responses={
            200: BrandSerializer,
            404: _not_found_response("brand", "Brand"),
        },
        auth=[],
    ),
)
class BrandViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    queryset = Brand.objects.filter(is_active=True).order_by("name")
    serializer_class = BrandSerializer
    lookup_field = "slug"
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]


@extend_schema_view(
    list=extend_schema(
        tags=["Catalog"],
        summary="List categories",
        description="Return all active product categories.",
        auth=[],
    ),
    retrieve=extend_schema(
        tags=["Catalog"],
        summary="Get category detail",
        description="Return a single active product category by slug.",
        parameters=[_slug_path_param("category")],
        responses={
            200: CategorySerializer,
            404: _not_found_response("category", "Category"),
        },
        auth=[],
    ),
)
class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    queryset = Category.objects.filter(is_active=True).order_by("name")
    serializer_class = CategorySerializer
    lookup_field = "slug"


@extend_schema_view(
    list=extend_schema(
        tags=["Catalog"],
        summary="List perfume notes",
        description="Return all active perfume notes. Supports `?search=` by name or category.",
        auth=[],
    ),
    retrieve=extend_schema(
        tags=["Catalog"],
        summary="Get perfume note detail",
        description="Return a single active perfume note by ID.",
        responses={
            200: PerfumeNoteSerializer,
            404: _not_found_response("perfume note", "PerfumeNote"),
        },
        auth=[],
    ),
)
class PerfumeNoteViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    queryset = PerfumeNote.objects.filter(is_active=True).order_by("name")
    serializer_class = PerfumeNoteSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "notes_category"]

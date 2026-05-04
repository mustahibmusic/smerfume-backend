from rest_framework.routers import DefaultRouter

from .views import BrandViewSet, CategoryViewSet, PerfumeNoteViewSet, ProductViewSet

router = DefaultRouter()
router.register("products", ProductViewSet, basename="product")
router.register("brands", BrandViewSet, basename="brand")
router.register("categories", CategoryViewSet, basename="category")
router.register("notes", PerfumeNoteViewSet, basename="note")

urlpatterns = router.urls

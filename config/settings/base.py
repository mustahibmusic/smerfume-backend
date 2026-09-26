from pathlib import Path
import os
from django.templatetags.static import static
from django.urls import reverse_lazy

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("SECRET_KEY")

DEBUG = False

ALLOWED_HOSTS = []


INSTALLED_APPS = [
    # Unfold Theme for Django Admin
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "unfold.contrib.import_export",

    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # GinIndex / trigram search (apps.catalog)

    # Third-party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "corsheaders",
    "storages",

    # Local Apps
    "apps.core",
    "apps.accounts",
    "apps.catalog",
    "apps.inventory",
    "apps.cart",
    "apps.orders",
    "apps.payments",
    "apps.shipping",
    "apps.offers",
    "apps.notifications",
    "apps.reviews",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/day",
        "user": "1000/day",
        "otp_request": "5/hour",
        "otp_verify": "10/minute",
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 24,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# ── API Documentation (drf-spectacular) ───────────────────────────────────────
SPECTACULAR_SETTINGS = {
    "TITLE": "Smerfume API",
    "DESCRIPTION": (
        "REST API for the Smerfume e-commerce fragrance platform.\n\n"
        "## Authentication\n"
        "All protected endpoints require a JWT Bearer token.\n"
        "1. Call `POST /api/auth/otp/request/` with your mobile number.\n"
        "2. Submit the OTP to `POST /api/auth/otp/verify/` to receive tokens.\n"
        "3. Click **Authorize** above and enter: `Bearer <access_token>`"
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "Auth", "description": "OTP-based passwordless authentication and token management"},
        {"name": "Catalog", "description": "Products, editions, variants, brands, categories and perfume notes"},
        {"name": "Cart", "description": "Shopping cart management"},
        {"name": "Orders", "description": "Checkout and order history"},
    ],
    "SECURITY": [{"BearerAuth": []}],
    # APPEND_COMPONENTS (not COMPONENTS) is the drf-spectacular key that merges
    # custom entries into the generated components section.
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Paste the access token returned by /api/auth/otp/verify/",
            }
        }
    },
    "SWAGGER_UI_SETTINGS": {
        "persistAuthorization": True,   # token survives page refresh during testing
        "displayRequestDuration": True,
        "filter": True,
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── File Storage ───────────────────────────────────────────────────────────────
# Set USE_S3=True in your environment to switch to Amazon S3.
# Required env vars when USE_S3 is True:
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_STORAGE_BUCKET_NAME,
#   AWS_S3_REGION_NAME (default: ap-south-1)
if os.getenv("USE_S3") == "True":
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "ap-south-1")
    AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com"
    AWS_DEFAULT_ACL = None          # inherit bucket policy (Block Public Access)
    AWS_S3_FILE_OVERWRITE = False   # keep original filename on re-upload

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "access_key": AWS_ACCESS_KEY_ID,
                "secret_key": AWS_SECRET_ACCESS_KEY,
                "bucket_name": AWS_STORAGE_BUCKET_NAME,
                "region_name": AWS_S3_REGION_NAME,
                "custom_domain": AWS_S3_CUSTOM_DOMAIN,
                "file_overwrite": AWS_S3_FILE_OVERWRITE,
                "default_acl": AWS_DEFAULT_ACL,
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/"


# ── SMS Backend ────────────────────────────────────────────────────────────
# SEND_REAL_OTP (bool env var, default False):
#   Master cost-control switch. When False, ConsoleSMSBackend is used
#   regardless of SMS_BACKEND — prevents accidental SMS sends in dev/staging.
#   Set SEND_REAL_OTP=True in production .env only.
#
# SMS_BACKEND (str env var):
#   Fully qualified class path of the SMS provider to use when SEND_REAL_OTP=True.
#   To switch providers, change this env var — no code changes needed.
#   Current provider: MSG91 (apps.accounts.providers.msg91.MSG91SMSBackend)
#   To switch to another: create provider class, set SMS_BACKEND to its path.
SEND_REAL_OTP = os.getenv("SEND_REAL_OTP", "False") == "True"
SMS_BACKEND = os.getenv(
    "SMS_BACKEND",
    "apps.accounts.providers.msg91.MSG91SMSBackend",
)
# MSG91 credentials — only required when SMS_BACKEND=MSG91SMSBackend
MSG91_AUTH_KEY = os.getenv("MSG91_AUTH_KEY", "")
MSG91_TEMPLATE_ID = os.getenv("MSG91_TEMPLATE_ID", "")
MSG91_SENDER_ID = os.getenv("MSG91_SENDER_ID", "SMRFME")


# ── Parfumly catalogue import ──────────────────────────────────────────────────
# Public, unauthenticated fragrance metadata API used only by the
# `import_parfumly_brand` management command (apps.catalog.parfumly).
# No API key exists or is required. The delay throttles consecutive requests
# because Parfumly rate-limits clients.
PARFUMLY_API_BASE_URL = os.getenv("PARFUMLY_API_BASE_URL", "https://api.parfumly.in")
PARFUMLY_REQUEST_DELAY_SECONDS = float(os.getenv("PARFUMLY_REQUEST_DELAY_SECONDS", "0.5"))
PARFUMLY_TIMEOUT_SECONDS = float(os.getenv("PARFUMLY_TIMEOUT_SECONDS", "20"))


# Admin environment badge (e.g. LOCAL, DEV, UAT, STAGING). Empty = no badge,
# so production shows nothing unless explicitly configured.
ADMIN_ENVIRONMENT = os.getenv("ADMIN_ENVIRONMENT", "")

# Target of the admin's "View site" link (the storefront, not this backend).
ADMIN_SITE_URL = os.getenv("ADMIN_SITE_URL", "https://smerfume.com")


def admin_environment_callback(request):
    """Return Unfold's [label, colour] pair for the header badge, or None."""
    from django.conf import settings

    label = (settings.ADMIN_ENVIRONMENT or "").strip().upper()
    if not label:
        return None
    return [label, "info" if label in ("LOCAL", "DEV") else "warning"]


def _nav_item(title, icon, url_name, perm):
    return {
        "title": title,
        "icon": icon,
        "link": reverse_lazy(url_name),
        "permission": lambda request: request.user.has_perm(perm),
    }


UNFOLD = {
    "SITE_TITLE": "Smerfume Admin",
    "SITE_HEADER": "Smerfume Backend",
    "SITE_SYMBOL": "local_mall",
    "SITE_URL": ADMIN_SITE_URL,
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "ENVIRONMENT": admin_environment_callback,
    "SIDEBAR": {
        "show_search": True,
        "command_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "items": [
                    {
                        "title": "Dashboard",
                        "icon": "dashboard",
                        "link": reverse_lazy("admin:index"),
                    },
                ],
            },
            {
                "title": "Sales",
                "items": [
                    _nav_item("Orders", "receipt_long", "admin:orders_order_changelist", "orders.view_order"),
                    _nav_item(
                        "New In-store Sale", "point_of_sale",
                        "admin:orders_order_new_in_store_sale", "orders.add_order",
                    ),
                    _nav_item("Returns", "assignment_return", "admin:orders_return_changelist", "orders.view_return"),
                    _nav_item("Refunds", "currency_exchange", "admin:orders_refund_changelist", "orders.view_refund"),
                ],
            },
            {
                "title": "Catalog",
                "items": [
                    _nav_item("Products", "inventory_2", "admin:catalog_product_changelist", "catalog.view_product"),
                    _nav_item(
                        "Editions", "collections_bookmark",
                        "admin:catalog_productedition_changelist", "catalog.view_productedition",
                    ),
                    _nav_item(
                        "Variants", "style",
                        "admin:catalog_productvariant_changelist", "catalog.view_productvariant",
                    ),
                    _nav_item("Brands", "sell", "admin:catalog_brand_changelist", "catalog.view_brand"),
                    _nav_item(
                        "Perfume Notes", "spa",
                        "admin:catalog_perfumenote_changelist", "catalog.view_perfumenote",
                    ),
                ],
            },
            {
                "title": "Inventory",
                "items": [
                    _nav_item(
                        "Stock", "warehouse",
                        "admin:inventory_inventorystock_changelist", "inventory.view_inventorystock",
                    ),
                    _nav_item(
                        "Partial Bottles", "water_drop",
                        "admin:inventory_partialbottlelot_changelist", "inventory.view_partialbottlelot",
                    ),
                    _nav_item(
                        "Stock Movements", "swap_horiz",
                        "admin:inventory_stockmovement_changelist", "inventory.view_stockmovement",
                    ),
                    _nav_item(
                        "Reservations", "bookmark_added",
                        "admin:inventory_stockreservation_changelist", "inventory.view_stockreservation",
                    ),
                    _nav_item(
                        "Warehouses", "store",
                        "admin:inventory_warehouse_changelist", "inventory.view_warehouse",
                    ),
                ],
            },
            {
                "title": "Customers",
                "items": [
                    _nav_item(
                        "Customers", "groups",
                        "admin:accounts_customerprofile_changelist", "accounts.view_customerprofile",
                    ),
                    _nav_item("Users", "manage_accounts", "admin:accounts_user_changelist", "accounts.view_user"),
                ],
            },
        ],
    },
    "SITE_ICON": {
        "light": lambda request: static("images/brand/smerfume_bottle_trans_logo.svg"),
        "dark": lambda request: static("images/brand/smerfume_bottle_trans_logo.svg"),
    },
    "SITE_LOGO": {
        "light": lambda request: static("images/brand/smerfume_trans_logo_light.svg"),
        "dark": lambda request: static("images/brand/smerfume_trans_logo_light.svg"),
    },
}

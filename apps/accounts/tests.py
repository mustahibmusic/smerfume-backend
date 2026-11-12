"""
Comprehensive tests for apps.accounts.

Test groups:
    OTPServiceTests      — unit tests for services/otp.py
    RequestOTPViewTests  — API tests for POST /otp/request/
    VerifyOTPViewTests   — API tests for POST /otp/verify/
    ResendOTPViewTests   — API tests for POST /otp/resend/
    LogoutViewTests      — API tests for POST /logout/
    MeViewTests          — API tests for GET/PATCH /me/
    CartMergeOnLoginTests — guest cart merged into user cart on OTP verify

All tests mock _get_sms_backend so no real SMS is ever dispatched.
"""

import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from redis.exceptions import RedisError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import OTPVerification
from apps.accounts.services import otp as otp_service
from apps.accounts.services.otp import (
    OTPRateLimitError,
    _generate_otp,
    create_otp_record,
    resend_otp,
    verify_otp_record,
)

User = get_user_model()

# Patch target — used in every test that touches the OTP service
_SMS_BACKEND_PATCH = "apps.accounts.services.otp._get_sms_backend"


def _mock_sms_backend():
    """Return a MagicMock backend that always reports SMS success."""
    mock = MagicMock()
    mock.return_value.send_otp.return_value = True
    return mock


def _make_user(mobile="+919876543210", **kwargs):
    """Create a minimal test user.

    Uses User.objects.create() directly (not create_user) so email stays
    NULL rather than being normalised to "" by AbstractBaseUser. This allows
    multiple test users to coexist without violating the email unique constraint.
    """
    username = kwargs.pop("username", f"u_{mobile[-4:]}")
    user = User(
        username=username,
        mobile_number=mobile,
        email=None,
        role="customer",
        is_staff=False,
    )
    user.set_unusable_password()
    for attr, val in kwargs.items():
        setattr(user, attr, val)
    user.save()
    return user


def _get_tokens(user):
    """Return (access_str, refresh_str) for a user."""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


# ── OTP service unit tests ───────────────────────────────────────────────────

class OTPServiceTests(TestCase):

    def setUp(self):
        self.user = _make_user()

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_generate_otp_is_6_digits(self, _mock):
        otp = _generate_otp()
        self.assertEqual(len(otp), 6)
        self.assertTrue(otp.isdigit())

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_create_otp_record_hashes_otp(self, _mock):
        """The stored otp_hash must not equal the plaintext OTP."""
        record, _ = create_otp_record(self.user, is_new_user=False)
        # The hash should not be a 6-digit number
        self.assertNotRegex(record.otp_hash, r"^\d{6}$")
        self.assertGreater(len(record.otp_hash), 6)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_verify_otp_record_success(self, _mock):
        """A correct OTP marks the record as verified."""
        from django.contrib.auth.hashers import make_password
        otp_code = "123456"
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password(otp_code),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        verified = verify_otp_record(str(record.otp_session_token), otp_code)
        self.assertTrue(verified.is_verified)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_verify_otp_wrong_otp_increments_attempt_count(self, _mock):
        """Submitting a wrong OTP increments attempt_count."""
        from django.contrib.auth.hashers import make_password
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password("999999"),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        with self.assertRaises(ValueError):
            verify_otp_record(str(record.otp_session_token), "000000")
        record.refresh_from_db()
        self.assertEqual(record.attempt_count, 1)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_verify_otp_locks_after_max_attempts(self, _mock):
        """After MAX_ATTEMPTS wrong attempts, is_locked is True."""
        from django.contrib.auth.hashers import make_password
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password("999999"),
            attempt_count=OTPVerification.MAX_ATTEMPTS,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertTrue(record.is_locked)
        with self.assertRaises(ValueError) as ctx:
            verify_otp_record(str(record.otp_session_token), "000000")
        self.assertIn("Too many", str(ctx.exception))

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_verify_otp_expired_raises(self, _mock):
        """An expired OTP raises ValueError."""
        from django.contrib.auth.hashers import make_password
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password("123456"),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        with self.assertRaises(ValueError) as ctx:
            verify_otp_record(str(record.otp_session_token), "123456")
        self.assertIn("expired", str(ctx.exception))

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_verify_otp_already_used_raises(self, _mock):
        """A previously verified OTP raises ValueError."""
        from django.contrib.auth.hashers import make_password
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password("123456"),
            is_verified=True,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        with self.assertRaises(ValueError) as ctx:
            verify_otp_record(str(record.otp_session_token), "123456")
        self.assertIn("already been used", str(ctx.exception))

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_otp_success(self, _mock):
        """resend_otp returns a record and sms_sent=True."""
        record, _ = create_otp_record(self.user, is_new_user=False)
        new_record, sms_sent = resend_otp(str(record.otp_session_token))
        self.assertTrue(sms_sent)
        self.assertEqual(new_record.resend_count, 1)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_otp_cooldown_enforced(self, _mock):
        """resend_otp raises ValueError when called within cooldown window."""
        from django.contrib.auth.hashers import make_password
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password("123456"),
            expires_at=timezone.now() + timedelta(minutes=5),
            last_resend_at=timezone.now(),  # just resent
        )
        with self.assertRaises(ValueError) as ctx:
            resend_otp(str(record.otp_session_token))
        self.assertIn("wait", str(ctx.exception).lower())

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_otp_limit_enforced(self, _mock):
        """resend_otp raises ValueError when MAX_RESENDS is reached."""
        from django.contrib.auth.hashers import make_password
        record = OTPVerification.objects.create(
            user=self.user,
            otp_hash=make_password("123456"),
            expires_at=timezone.now() + timedelta(minutes=5),
            resend_count=OTPVerification.MAX_RESENDS,
        )
        with self.assertRaises(ValueError) as ctx:
            resend_otp(str(record.otp_session_token))
        self.assertIn("limit", str(ctx.exception).lower())


# ── RequestOTPView tests ─────────────────────────────────────────────────────

class RequestOTPViewTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("otp_request")

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_valid_mobile_returns_200_with_session_token(self, _mock):
        resp = self.client.post(self.url, {"mobile_number": "9876543210"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("otp_session_token", resp.data)

    def test_invalid_mobile_returns_400(self):
        resp = self.client.post(self.url, {"mobile_number": "12345"})
        self.assertEqual(resp.status_code, 400)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_new_user_created_on_first_request(self, _mock):
        self.assertFalse(User.objects.filter(mobile_number="+919000000001").exists())
        self.client.post(self.url, {"mobile_number": "9000000001"})
        self.assertTrue(User.objects.filter(mobile_number="+919000000001").exists())

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_existing_user_not_duplicated(self, _mock):
        _make_user(mobile="+919000000002")
        self.client.post(self.url, {"mobile_number": "9000000002"})
        self.assertEqual(User.objects.filter(mobile_number="+919000000002").count(), 1)


# ── VerifyOTPView tests ──────────────────────────────────────────────────────

class VerifyOTPViewTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("otp_verify")
        self.user = _make_user(mobile="+919111111111")

    def _create_record(self, otp_code="654321", **kwargs):
        from django.contrib.auth.hashers import make_password
        defaults = dict(
            user=self.user,
            otp_hash=make_password(otp_code),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        defaults.update(kwargs)
        return OTPVerification.objects.create(**defaults)

    def test_correct_otp_returns_tokens_and_user(self):
        record = self._create_record()
        resp = self.client.post(self.url, {
            "otp_session_token": str(record.otp_session_token),
            "otp": "654321",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        self.assertIn("user", resp.data)

    def test_response_contains_public_id_not_integer_id(self):
        record = self._create_record()
        resp = self.client.post(self.url, {
            "otp_session_token": str(record.otp_session_token),
            "otp": "654321",
        })
        self.assertEqual(resp.status_code, 200)
        user_data = resp.data["user"]
        self.assertIn("public_id", user_data)
        self.assertNotIn("id", user_data)
        # public_id should be a UUID string, not an integer
        import uuid
        uuid.UUID(str(user_data["public_id"]))  # raises if invalid

    def test_wrong_otp_returns_400(self):
        record = self._create_record()
        resp = self.client.post(self.url, {
            "otp_session_token": str(record.otp_session_token),
            "otp": "000000",
        })
        self.assertEqual(resp.status_code, 400)

    def test_expired_otp_returns_400(self):
        record = self._create_record(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        resp = self.client.post(self.url, {
            "otp_session_token": str(record.otp_session_token),
            "otp": "654321",
        })
        self.assertEqual(resp.status_code, 400)

    def test_invalid_session_token_returns_400(self):
        import uuid
        resp = self.client.post(self.url, {
            "otp_session_token": str(uuid.uuid4()),
            "otp": "123456",
        })
        self.assertEqual(resp.status_code, 400)

    def test_already_used_otp_returns_400(self):
        record = self._create_record(is_verified=True)
        resp = self.client.post(self.url, {
            "otp_session_token": str(record.otp_session_token),
            "otp": "654321",
        })
        self.assertEqual(resp.status_code, 400)


# ── ResendOTPView tests ──────────────────────────────────────────────────────

class ResendOTPViewTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("otp_resend")
        self.user = _make_user(mobile="+919222222222")

    def _create_record(self, **kwargs):
        from django.contrib.auth.hashers import make_password
        defaults = dict(
            user=self.user,
            otp_hash=make_password("111111"),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        defaults.update(kwargs)
        return OTPVerification.objects.create(**defaults)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_success(self, _mock):
        record = self._create_record()
        resp = self.client.post(self.url, {"otp_session_token": str(record.otp_session_token)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("detail", resp.data)

    def test_resend_during_cooldown_returns_400(self):
        record = self._create_record(last_resend_at=timezone.now())
        resp = self.client.post(self.url, {"otp_session_token": str(record.otp_session_token)})
        self.assertEqual(resp.status_code, 400)

    def test_resend_limit_returns_400(self):
        record = self._create_record(resend_count=OTPVerification.MAX_RESENDS)
        resp = self.client.post(self.url, {"otp_session_token": str(record.otp_session_token)})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_session_token_returns_400(self):
        import uuid
        resp = self.client.post(self.url, {"otp_session_token": str(uuid.uuid4())})
        self.assertEqual(resp.status_code, 400)


# ── LogoutView tests ─────────────────────────────────────────────────────────

class LogoutViewTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("logout")
        self.user = _make_user(mobile="+919333333333")

    def test_logout_blacklists_refresh_token(self):
        access, refresh = _get_tokens(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = self.client.post(self.url, {"refresh": refresh})
        self.assertEqual(resp.status_code, 200)

    def test_blacklisted_token_cannot_refresh(self):
        access, refresh = _get_tokens(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        # Blacklist the token
        self.client.post(self.url, {"refresh": refresh})
        # Try to use it — should fail
        refresh_url = reverse("token_refresh")
        resp = self.client.post(refresh_url, {"refresh": refresh})
        self.assertEqual(resp.status_code, 401)

    def test_logout_without_token_returns_400(self):
        access, _ = _get_tokens(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = self.client.post(self.url, {})
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_logout_returns_401(self):
        resp = self.client.post(self.url, {"refresh": "sometoken"})
        self.assertEqual(resp.status_code, 401)


# ── MeView tests ─────────────────────────────────────────────────────────────

class MeViewTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("me")
        self.user = _make_user(mobile="+919444444444")

    def _auth(self):
        access, _ = _get_tokens(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_get_me_returns_public_id_not_integer(self):
        self._auth()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("public_id", resp.data)
        self.assertNotIn("id", resp.data)
        import uuid
        uuid.UUID(str(resp.data["public_id"]))

    def test_get_me_unauthenticated_returns_401(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 401)

    def test_patch_me_updates_name(self):
        self._auth()
        resp = self.client.patch(self.url, {"first_name": "Jasmine", "last_name": "Shah"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Jasmine")
        self.assertEqual(self.user.last_name, "Shah")

    def test_patch_me_duplicate_email_returns_400(self):
        other = _make_user(mobile="+919555555555", username="other_user")
        other.email = "taken@example.com"
        other.save()

        self._auth()
        resp = self.client.patch(self.url, {"email": "taken@example.com"})
        self.assertEqual(resp.status_code, 400)


# ── Cart merge on OTP verify ─────────────────────────────────────────────────

# A guest cart token in the generated format (32 lowercase hex characters).
MERGE_CART_TOKEN = "0123456789abcdef0123456789abcdef"


class CartMergeOnLoginTests(TestCase):
    """
    Verify that a guest cart is merged into the user's cart when
    X-Cart-Token is present at POST /api/auth/otp/verify/.
    """

    VERIFY_URL = "/api/auth/otp/verify/"
    REQUEST_URL = "/api/auth/otp/request/"

    def setUp(self):
        from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
        from apps.cart.models import Cart, CartItem

        self.client = APIClient()
        self.user = _make_user(mobile="+919600000001")

        brand = Brand.objects.get_or_create(name="MergeBrand", slug="mergebrand")[0]
        cat = Category.objects.get_or_create(name="MergeCat", slug="mergecat")[0]
        product = Product.objects.get_or_create(
            name="MergeProd", slug="mergeprod",
            defaults={"brand": brand, "category": cat},
        )[0]
        edition = ProductEdition.objects.get_or_create(
            product=product, slug="mergeprod-edp",
            defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
        )[0]
        self.variant = ProductVariant.objects.create(
            edition=edition, size_ml=10, selling_price="200.00", mrp="250.00",
            sku="TEST-CARTMERGE-10ML",
        )

        self.guest_cart = Cart.objects.create(session_key=MERGE_CART_TOKEN)
        CartItem.objects.create(cart=self.guest_cart, variant=self.variant, quantity=3)

    def _do_otp_verify(self, user, otp_code, cart_token=None):
        """Create an OTP record and call the verify endpoint."""
        from django.contrib.auth.hashers import make_password
        from apps.accounts.models import OTPVerification

        record = OTPVerification.objects.create(
            user=user,
            otp_hash=make_password(otp_code),
            purpose="login",
            is_new_user=False,
        )
        kwargs = {"data": {"otp_session_token": str(record.otp_session_token), "otp": otp_code}}
        if cart_token:
            kwargs["HTTP_X_CART_TOKEN"] = cart_token
        return self.client.post(self.VERIFY_URL, **kwargs)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_guest_cart_merged_on_verify(self, _mock):
        """Guest cart items are in user cart after OTP verify with X-Cart-Token."""
        from apps.cart.models import Cart

        resp = self._do_otp_verify(self.user, "111111", cart_token=MERGE_CART_TOKEN)
        self.assertEqual(resp.status_code, 200)

        user_cart = Cart.objects.get(user=self.user)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().quantity, 3)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_guest_cart_deleted_after_merge(self, _mock):
        from apps.cart.models import Cart

        self._do_otp_verify(self.user, "222222", cart_token=MERGE_CART_TOKEN)
        self.assertFalse(Cart.objects.filter(session_key=MERGE_CART_TOKEN).exists())

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_verify_without_cart_token_succeeds_normally(self, _mock):
        """OTP verify without X-Cart-Token still returns tokens (no merge attempted)."""
        resp = self._do_otp_verify(self.user, "333333")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)


# ── Redis throttle failure policy ─────────────────────────────────────────────

def _broken_cache():
    broken = MagicMock()
    broken.get.side_effect = RedisError("connection refused")
    broken.set.side_effect = RedisError("connection refused")
    return broken


_OTP_THROTTLE = "apps.core.throttling.FailClosedScopedRateThrottle"


class OTPThrottleFailClosedTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _assert_503(self, resp, logs, scope):
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["detail"], "Login is temporarily unavailable. Please try again shortly.")
        self.assertIn(f"scope={scope}", logs.output[0])
        self.assertNotIn("connection refused", resp.content.decode())

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_request_fails_closed(self, sms):
        with patch(f"{_OTP_THROTTLE}.cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = self.client.post(reverse("otp_request"), {"mobile_number": "9000000101"})
        self._assert_503(resp, logs, "otp_request")
        self.assertEqual(OTPVerification.objects.count(), 0)
        sms.assert_not_called()

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_fails_closed(self, sms):
        user = _make_user(mobile="+919000000102")
        record, _ = create_otp_record(user)
        sms.reset_mock()
        with patch(f"{_OTP_THROTTLE}.cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = self.client.post(
                    reverse("otp_resend"), {"otp_session_token": str(record.otp_session_token)}
                )
        self._assert_503(resp, logs, "otp_request")
        record.refresh_from_db()
        self.assertEqual(record.resend_count, 0)
        sms.assert_not_called()

    def test_verify_fails_closed(self):
        with patch(f"{_OTP_THROTTLE}.cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = self.client.post(
                    reverse("otp_verify"),
                    {"otp_session_token": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "otp": "123456"},
                )
        self._assert_503(resp, logs, "otp_verify")


class OTPThrottleSharingTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "otp_request": "1/hour"}
        patcher = patch(f"{_OTP_THROTTLE}.THROTTLE_RATES", rates)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_shares_request_counter(self, _sms):
        resp = self.client.post(reverse("otp_request"), {"mobile_number": "9000000103"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            reverse("otp_resend"), {"otp_session_token": resp.data["otp_session_token"]}
        )
        self.assertEqual(resp.status_code, 429)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_num_proxies_separates_clients(self, _sms):
        url = reverse("otp_request")
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}):
            first = self.client.post(url, {"mobile_number": "9000000104"}, HTTP_X_FORWARDED_FOR="1.1.1.1")
            other = self.client.post(url, {"mobile_number": "9000000105"}, HTTP_X_FORWARDED_FOR="2.2.2.2")
            again = self.client.post(url, {"mobile_number": "9000000106"}, HTTP_X_FORWARDED_FOR="1.1.1.1")
        self.assertEqual((first.status_code, other.status_code, again.status_code), (200, 200, 429))


# ── Per-mobile OTP send limit ─────────────────────────────────────────────────

_LIMIT_MESSAGE = "Too many OTP requests for this number. Please try again later."


def _sent_row(user, *, created_ago=None, resend_count=0, resent_ago=None):
    """OTPVerification row with explicit send timestamps (created_at is auto_now_add)."""
    now = timezone.now()
    row = OTPVerification.objects.create(
        user=user, otp_hash="x", expires_at=now + timedelta(minutes=5),
        resend_count=resend_count,
        last_resend_at=(now - resent_ago) if resent_ago is not None else None,
    )
    if created_ago is not None:
        OTPVerification.objects.filter(pk=row.pk).update(created_at=now - created_ago)
    return row


class MobileSendLimitTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = _make_user(mobile="+919333333333")

    def test_counts_initial_sends_and_resends(self):
        _sent_row(self.user, resend_count=2, resent_ago=timedelta(minutes=1))
        _sent_row(self.user)
        self.assertEqual(otp_service._recent_send_count(self.user), 4)

    def test_ignores_sends_older_than_one_hour(self):
        _sent_row(self.user, created_ago=timedelta(hours=2), resend_count=3,
                  resent_ago=timedelta(minutes=90))
        self.assertEqual(otp_service._recent_send_count(self.user), 0)

    def test_recent_resend_on_old_row_counts(self):
        _sent_row(self.user, created_ago=timedelta(hours=2), resend_count=1,
                  resent_ago=timedelta(minutes=10))
        self.assertEqual(otp_service._recent_send_count(self.user), 1)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_sixth_request_is_refused(self, sms):
        for _ in range(5):
            create_otp_record(self.user)
        sms.reset_mock()
        with self.assertRaises(OTPRateLimitError):
            create_otp_record(self.user)
        self.assertEqual(OTPVerification.objects.filter(user=self.user).count(), 5)
        sms.assert_not_called()

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resends_count_toward_limit(self, _sms):
        _sent_row(self.user)
        row = _sent_row(self.user, resend_count=2, resent_ago=timedelta(minutes=1))
        _sent_row(self.user)  # 1 + 3 + 1 = 5 sends
        with self.assertRaises(OTPRateLimitError):
            resend_otp(str(row.otp_session_token))
        row.refresh_from_db()
        self.assertEqual(row.resend_count, 2)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_other_number_not_affected(self, _sms):
        for _ in range(5):
            create_otp_record(self.user)
        other = _make_user(mobile="+919333333334", username="u_other")
        create_otp_record(other)  # does not raise

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_views_return_generic_429(self, _sms):
        for _ in range(5):
            create_otp_record(self.user)
        client = APIClient()
        resp = client.post(reverse("otp_request"), {"mobile_number": "9333333333"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["error"], _LIMIT_MESSAGE)
        token = OTPVerification.objects.filter(user=self.user).first().otp_session_token
        resp = client.post(reverse("otp_resend"), {"otp_session_token": str(token)})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["error"], _LIMIT_MESSAGE)


class MobileSendLimitConcurrencyTests(TransactionTestCase):
    """Concurrent sends for one mobile must serialize on the user-row lock
    before counting, deciding, writing OTP state and sending the SMS.
    _recent_send_count is slowed so that, without the lock, both threads
    would read the same stale count and both pass."""

    def setUp(self):
        self.user = _make_user(mobile="+919444444444")

    def _run_parallel(self, *calls):
        results = [None] * len(calls)
        barrier = threading.Barrier(len(calls))

        def _run(index, call):
            barrier.wait()
            try:
                call()
                results[index] = "ok"
            except OTPRateLimitError as exc:
                results[index] = type(exc).__name__
            except Exception as exc:  # deadlock/serialization must surface
                results[index] = f"db:{type(exc).__name__}: {exc}"
            finally:
                connection.close()

        threads = [threading.Thread(target=_run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def _slow_count(self):
        real = otp_service._recent_send_count

        def slow(user):
            count = real(user)
            time.sleep(0.3)  # widen the stale-read window
            return count

        return patch("apps.accounts.services.otp._recent_send_count", side_effect=slow)

    def test_two_requests_at_limit_minus_one(self):
        _sent_row(self.user, resend_count=3, resent_ago=timedelta(minutes=1))  # 4 sends
        with patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend) as sms, self._slow_count():
            results = self._run_parallel(
                lambda: create_otp_record(self.user),
                lambda: create_otp_record(self.user),
            )
        self.assertEqual(sorted(results), ["OTPRateLimitError", "ok"], results)
        self.assertEqual(otp_service._recent_send_count(self.user), 5)
        self.assertEqual(sms.call_count, 1)

    def test_request_and_resend_at_limit_minus_one(self):
        row = _sent_row(self.user, resend_count=2, resent_ago=timedelta(minutes=1))  # 3 sends
        _sent_row(self.user)  # 4 sends
        with patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend) as sms, self._slow_count():
            results = self._run_parallel(
                lambda: create_otp_record(self.user),
                lambda: resend_otp(str(row.otp_session_token)),
            )
        self.assertEqual(sorted(results), ["OTPRateLimitError", "ok"], results)
        self.assertEqual(otp_service._recent_send_count(self.user), 5)
        self.assertEqual(sms.call_count, 1)


# ── Admin ERP Phase 1: user privilege safety ─────────────────────────────────

from django.contrib import admin as django_admin  # noqa: E402
from django.contrib.auth.models import Permission  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from apps.accounts.admin import UserAdmin  # noqa: E402

PRIVILEGE_FIELDS = ("is_superuser", "is_staff", "groups", "user_permissions", "role")


class UserAdminPrivilegeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(
            username="accountmgr", email="accountmgr@example.com", password="testpass123",
            is_staff=True, role="staff",
        )
        self.manager.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="accounts",
            codename__in=["view_user", "add_user", "change_user", "delete_user"],
        ))
        self.superuser = User.objects.create_superuser(
            username="rootadmin", email="rootadmin@example.com", password="testpass123",
        )
        self.customer = _make_user(mobile="+919811100001", username="plaincustomer")
        self.other_staff = User.objects.create_user(
            username="otherstaff", email="otherstaff@example.com", password="testpass123",
            is_staff=True, role="staff",
        )
        self.admin_role_user = _make_user(mobile="+919811100002", username="apiadmin", role="admin")
        self.model_admin = UserAdmin(User, django_admin.site)

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_privilege_fields_readonly_for_non_superuser(self):
        request = self._request(self.manager)
        readonly = self.model_admin.get_readonly_fields(request, self.customer)
        for field in PRIVILEGE_FIELDS:
            self.assertIn(field, readonly)
        form_class = self.model_admin.get_form(request, self.customer)
        for field in PRIVILEGE_FIELDS:
            self.assertNotIn(field, form_class.base_fields)

    def test_privilege_fields_editable_for_superuser(self):
        request = self._request(self.superuser)
        readonly = self.model_admin.get_readonly_fields(request, self.customer)
        for field in PRIVILEGE_FIELDS:
            self.assertNotIn(field, readonly)

    def test_non_superuser_can_manage_customers_only(self):
        request = self._request(self.manager)
        self.assertTrue(self.model_admin.has_change_permission(request, self.customer))
        for target in (self.superuser, self.other_staff, self.admin_role_user, self.manager):
            self.assertFalse(self.model_admin.has_change_permission(request, target), target)
            self.assertFalse(self.model_admin.has_delete_permission(request, target), target)
        self.assertTrue(self.model_admin.has_change_permission(request))

    def test_superuser_can_manage_staff(self):
        request = self._request(self.superuser)
        self.assertTrue(self.model_admin.has_change_permission(request, self.other_staff))

    def test_add_form_has_no_role_for_non_superuser(self):
        fields = [
            f for _, opts in self.model_admin.get_fieldsets(self._request(self.manager))
            for f in opts["fields"]
        ]
        self.assertNotIn("role", fields)
        superuser_fields = [
            f for _, opts in self.model_admin.get_fieldsets(self._request(self.superuser))
            for f in opts["fields"]
        ]
        self.assertIn("role", superuser_fields)

    def test_non_superuser_cannot_reset_staff_password_or_edit_superuser(self):
        self.client.force_login(self.manager)
        password_url = reverse("admin:auth_user_password_change", args=[self.superuser.pk])
        self.assertEqual(self.client.get(password_url).status_code, 403)
        change_url = reverse("admin:accounts_user_change", args=[self.superuser.pk])
        self.client.post(change_url, {"email": "taken@example.com"})
        self.superuser.refresh_from_db()
        self.assertEqual(self.superuser.email, "rootadmin@example.com")

    def test_non_superuser_cannot_escalate_customer(self):
        self.client.force_login(self.manager)
        url = reverse("admin:accounts_user_change", args=[self.customer.pk])
        self.client.post(url, {
            "username": self.customer.username, "mobile_number": self.customer.mobile_number,
            "email": "", "first_name": "", "last_name": "",
            "is_superuser": "on", "is_staff": "on", "role": "admin", "is_active": "on",
        })
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_superuser)
        self.assertFalse(self.customer.is_staff)
        self.assertEqual(self.customer.role, "customer")


# ── Admin ERP Phase 2: changelist query counts ───────────────────────────────

from apps.accounts.models import CustomerProfile  # noqa: E402
from apps.core.test_utils import AdminQueryCountMixin  # noqa: E402


class AccountsAdminQueryCountTests(AdminQueryCountMixin, TestCase):
    def test_customer_profile_changelist(self):
        self.client.force_login(get_user_model().objects.create_superuser(
            username="acctquery", email="acctquery@example.com", password="testpass123",
        ))

        def make(i):
            user = _make_user(mobile=f"+91982000{i:04d}", username=f"qprof{i}")
            CustomerProfile.objects.get_or_create(user=user)
        self.assertChangelistQueriesFlat("/admin/accounts/customerprofile/", make)


# ── Admin ERP Phase 2: customer operations view ──────────────────────────────

from decimal import Decimal  # noqa: E402

from apps.orders.models import Order, OrderItem, Return, ShippingAddress  # noqa: E402


class CustomerOperationsViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser(
            username="custops", email="custops@example.com", password="testpass123",
        ))
        self.customer = _make_user(mobile="+919830000001", username="opscustomer")
        self.order = Order.objects.create(
            user=self.customer, order_number="SMR-CUST-000001",
            subtotal=Decimal("900.00"), total=Decimal("900.00"), status=Order.STATUS_DELIVERED,
        )
        ShippingAddress.objects.create(
            order=self.order, full_name="Ops Customer", mobile="9830000001",
            address_line1="7 Hill Road", city="Nashik", state="MH", pincode="422001",
        )
        self.return_request = Return.objects.create(order=self.order)
        other = _make_user(mobile="+919830000002", username="othercustomer")
        self.other_order = Order.objects.create(
            user=other, order_number="SMR-CUST-000002",
            subtotal=Decimal("100.00"), total=Decimal("100.00"),
        )

    def _assert_panel(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SMR-CUST-000001")
        self.assertContains(response, reverse("admin:orders_order_change", args=[self.order.pk]))
        self.assertContains(response, reverse("admin:orders_return_change", args=[self.return_request.pk]))
        self.assertContains(response, "Nashik")
        self.assertContains(response, f"user__id__exact={self.customer.pk}")
        self.assertNotContains(response, "SMR-CUST-000002")

    def test_user_change_page_shows_customer_activity(self):
        self._assert_panel(self.client.get(
            reverse("admin:accounts_user_change", args=[self.customer.pk])
        ))

    def test_customer_profile_page_shows_customer_activity(self):
        profile, _ = CustomerProfile.objects.get_or_create(user=self.customer)
        self._assert_panel(self.client.get(
            reverse("admin:accounts_customerprofile_change", args=[profile.pk])
        ))

    def test_view_all_orders_link_filters_by_customer(self):
        response = self.client.get(
            reverse("admin:orders_order_changelist"), {"user__id__exact": self.customer.pk}
        )
        self.assertEqual(set(response.context["cl"].result_list), {self.order})


# ── Admin ERP Phase 2: staff groups ──────────────────────────────────────────

from io import StringIO  # noqa: E402

from django.contrib.auth.models import Group  # noqa: E402
from django.core.management import CommandError, call_command  # noqa: E402

from apps.accounts.management.commands import setup_staff_groups as groups_command  # noqa: E402


def _group_codes(name):
    return {
        f"{p.content_type.app_label}.{p.codename}"
        for p in Group.objects.get(name=name).permissions.select_related("content_type")
    }


class SetupStaffGroupsTests(TestCase):
    def _run(self, *args):
        call_command("setup_staff_groups", *args, stdout=StringIO())

    def test_creates_four_groups_with_exact_permissions(self):
        self._run()
        self.assertEqual(
            set(Group.objects.values_list("name", flat=True)),
            {"Ops", "Warehouse", "Finance", "Manager"},
        )
        for name, codes in groups_command.group_permissions().items():
            self.assertEqual(_group_codes(name), set(codes), name)

    def test_least_privilege_boundaries(self):
        self._run()
        ops, warehouse, finance, manager = (
            _group_codes(n) for n in ("Ops", "Warehouse", "Finance", "Manager")
        )
        self.assertIn("orders.process_return", ops)
        self.assertNotIn("orders.receive_return", ops)
        self.assertIn("orders.receive_return", warehouse)
        self.assertNotIn("orders.process_return", warehouse)
        self.assertNotIn("orders.complete_refund", ops | warehouse)
        self.assertIn("orders.complete_refund", finance)
        self.assertNotIn("purchases.reverse_goodsreceipt", warehouse)
        self.assertIn("accounts.change_user", ops)
        self.assertIn("orders.change_return", finance)
        self.assertNotIn("orders.change_returnitem", finance)
        self.assertTrue((ops | warehouse | finance) <= manager)

    def test_never_grants_auth_admin_or_ledger_permissions(self):
        self._run()
        every = set().union(*(_group_codes(n) for n in ("Ops", "Warehouse", "Finance", "Manager")))
        for code in every:
            app_label, codename = code.split(".")
            self.assertNotIn(app_label, ("auth", "admin", "contenttypes", "sessions"), code)
            if codename.startswith("delete_"):
                self.assertIn(code, groups_command.DRAFT_INLINE_DELETES, code)
        for ledger in ("stockmovement", "stocktransaction", "stockreservation",
                       "stockreservationallocation", "bookedquantitychange"):
            for verb in ("add", "change", "delete"):
                self.assertFalse(any(c.endswith(f".{verb}_{ledger}") for c in every), ledger)

    def test_rerun_is_idempotent_and_removes_hand_added_permissions(self):
        self._run()
        ops = Group.objects.get(name="Ops")
        before = _group_codes("Ops")
        ops.permissions.add(Permission.objects.get(codename="complete_refund"))
        self._run()
        self.assertEqual(_group_codes("Ops"), before)
        self.assertEqual(Group.objects.filter(name="Ops").count(), 1)

    def test_users_are_not_touched(self):
        user = _make_user(mobile="+919840000001", username="groupmember")
        self._run()
        Group.objects.get(name="Ops").user_set.add(user)
        self._run()
        user.refresh_from_db()
        self.assertTrue(user.groups.filter(name="Ops").exists())
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)

    def test_missing_permission_fails_and_changes_nothing(self):
        broken = dict(groups_command.GROUP_PERMISSIONS)
        broken["Ops"] = broken["Ops"] + ("orders.no_such_permission",)
        with patch.object(groups_command, "GROUP_PERMISSIONS", broken):
            with self.assertRaisesMessage(CommandError, "orders.no_such_permission"):
                self._run()
        self.assertFalse(Group.objects.exists())

    def test_dry_run_changes_nothing(self):
        self._run("--dry-run")
        self.assertFalse(Group.objects.exists())


class SupplierPaymentGroupPermissionTests(TestCase):
    """DEC-010: Finance records and reverses supplier payments; only
    Manager classifies pre-existing POs; Warehouse and Ops get nothing."""

    def test_payment_permission_matrix(self):
        call_command("setup_staff_groups", stdout=StringIO())
        ops, warehouse, finance, manager = (
            _group_codes(n) for n in ("Ops", "Warehouse", "Finance", "Manager")
        )
        payment_perms = {
            "purchases.record_po_payment", "purchases.reverse_po_payment",
            "purchases.view_purchaseorderpayment",
        }
        self.assertTrue(payment_perms <= finance)
        self.assertNotIn("purchases.set_po_tax_treatment", finance)
        self.assertTrue(payment_perms | {"purchases.set_po_tax_treatment"} <= manager)
        for group in (ops, warehouse):
            self.assertFalse(group & (payment_perms | {"purchases.set_po_tax_treatment"}))

    def test_dec011_posting_and_paid_through_permissions(self):
        call_command("setup_staff_groups", stdout=StringIO())
        ops, warehouse, finance, manager = (
            _group_codes(n) for n in ("Ops", "Warehouse", "Finance", "Manager")
        )
        account_admin = {"purchases.add_paymentaccount", "purchases.change_paymentaccount"}
        self.assertTrue({"purchases.post_po_payment", "purchases.view_paymentaccount"} <= finance)
        self.assertFalse(finance & account_admin)
        self.assertTrue({"purchases.post_po_payment", "purchases.view_paymentaccount"} | account_admin <= manager)
        for group in (ops, warehouse, finance, manager):
            self.assertNotIn("purchases.delete_paymentaccount", group)
        for group in (ops, warehouse):
            self.assertFalse(group & ({"purchases.post_po_payment", "purchases.view_paymentaccount"} | account_admin))


# ── Contact-number canonicalisation ──────────────────────────────────────────

import importlib  # noqa: E402

from django.apps import apps as django_apps  # noqa: E402
from rest_framework import serializers as drf_serializers  # noqa: E402

from apps.accounts import mobile as mobile_rules  # noqa: E402
from apps.accounts.mobile_cleanup import canonicalize  # noqa: E402
from apps.accounts.serializers import _normalize_indian_mobile  # noqa: E402
from apps.cart.models import Cart  # noqa: E402
from apps.orders.services import resolve_customer_by_mobile  # noqa: E402


class MobileNormalizerTests(TestCase):
    def test_any_ten_digit_local_number_gets_plus_91(self):
        for raw, canonical in (
            ("0123456789", "+910123456789"), ("2123456789", "+912123456789"),
            ("5123456789", "+915123456789"), ("9876543210", "+919876543210"),
        ):
            self.assertEqual(mobile_rules.normalize_indian_mobile(raw), canonical)

    def test_equivalent_forms_are_one_number(self):
        for raw in ("9876543210", "09876543210", "919876543210", "+919876543210",
                    "+91 98765-43210", " (98765) 43210 ", "+91 09876543210"):
            self.assertEqual(mobile_rules.normalize_indian_mobile(raw), "+919876543210", raw)

    def test_only_the_extra_trunk_zero_is_dropped(self):
        self.assertEqual(mobile_rules.normalize_indian_mobile("00123456789"), "+910123456789")
        self.assertEqual(mobile_rules.normalize_indian_mobile("0123456789"), "+910123456789")

    def test_international_and_legacy_numbers_are_kept(self):
        self.assertEqual(mobile_rules.normalize_indian_mobile("+447911123456"), "+447911123456")
        self.assertEqual(mobile_rules.normalize_indian_mobile("+1 415 555 0100"), "+14155550100")
        self.assertEqual(mobile_rules.normalize_indian_mobile("1234567890123"), "1234567890123")

    def test_invalid_values(self):
        for raw in ("", "987654321", "98765abcde", "+1234", "1234567890123456", "+91987654321"):
            with self.assertRaises(mobile_rules.InvalidMobileNumber, msg=raw):
                mobile_rules.normalize_indian_mobile(raw)

    def test_otp_takes_local_numbers_only_with_any_first_digit(self):
        self.assertEqual(_normalize_indian_mobile("0123456789"), "+910123456789")
        self.assertEqual(_normalize_indian_mobile("+919876543210"), "+919876543210")
        with self.assertRaises(drf_serializers.ValidationError):
            _normalize_indian_mobile("+447911123456")

    def test_lookup_prefix(self):
        prefix = mobile_rules.mobile_lookup_prefix
        self.assertIsNone(prefix("98765"))
        self.assertEqual(prefix("987654"), "+91987654")
        self.assertEqual(prefix("012345"), "+91012345")  # a leading zero stays part of the number
        self.assertIsNone(prefix("+91 98765"))  # 5 local digits
        self.assertEqual(prefix("+91987654"), "+91987654")
        self.assertEqual(prefix("09876543210"), "+919876543210")
        self.assertEqual(prefix("919876543210"), "+919876543210")
        self.assertIsNone(prefix("+447911123456"))
        self.assertIsNone(prefix("98765abc"))

    def test_mask(self):
        self.assertEqual(mobile_rules.mask_mobile("+919876543210"), "******3210")


class ResolveCustomerByMobileTests(TestCase):
    def test_equivalent_forms_resolve_to_one_customer(self):
        first = resolve_customer_by_mobile("9876543210")
        self.assertEqual(first.mobile_number, "+919876543210")
        for raw in ("+919876543210", "09876543210", "919876543210"):
            self.assertEqual(resolve_customer_by_mobile(raw), first)
        self.assertEqual(User.objects.filter(mobile_number__endswith="9876543210").count(), 1)

    def test_international_number_kept(self):
        self.assertEqual(resolve_customer_by_mobile("+447911123456").mobile_number, "+447911123456")

    def test_new_customer_log_masks_mobile(self):
        with self.assertLogs("apps.orders.services", level="INFO") as logs:
            resolve_customer_by_mobile("9876500001")
        self.assertNotIn("9876500001", "".join(logs.output))
        self.assertIn("******0001", "".join(logs.output))


def _mobile_user(username, mobile, **extra):
    user = User(username=username, mobile_number=mobile, role="customer", email=None, **extra)
    user.set_unusable_password()
    user.save()
    return user


def _cart_variant():
    from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant

    brand = Brand.objects.create(name="MobBrand", slug="mobbrand")
    category = Category.objects.create(name="MobCat", slug="mobcat")
    product = Product.objects.create(name="MobProduct", slug="mobproduct", brand=brand, category=category)
    edition = ProductEdition.objects.create(product=product, name="EDP", concentration="edp", gender="unisex")
    return ProductVariant.objects.create(edition=edition, size_ml=50, sku="MOB-1", selling_price="1", mrp="1")


class MobileCanonicalisationTests(TestCase):
    def test_canonicalize_converts_and_skips_collisions_and_is_idempotent(self):
        bare = _mobile_user("bare", "9000000001")
        trunk = _mobile_user("trunk", "09000000002")
        zero_first = _mobile_user("zero", "0123456789")
        clash_a = _mobile_user("clash_a", "9000000003")
        clash_b = _mobile_user("clash_b", "+919000000003")
        foreign = _mobile_user("foreign", "+447911123456")
        legacy = _mobile_user("legacy", "1234567890123")

        result = canonicalize(User)
        self.assertEqual(result, {"updated": 3, "already": 0, "collisions": 1, "unrecognised": 2})
        for user, expected in (
            (bare, "+919000000001"), (trunk, "+919000000002"), (zero_first, "+910123456789"),
            (clash_a, "9000000003"), (clash_b, "+919000000003"),
            (foreign, "+447911123456"), (legacy, "1234567890123"),
        ):
            user.refresh_from_db()
            self.assertEqual(user.mobile_number, expected)
        self.assertEqual(canonicalize(User)["updated"], 0)  # a second run changes nothing

    def test_migration_function_uses_the_same_rules(self):
        _mobile_user("mig", "9000000009")
        migration = importlib.import_module("apps.accounts.migrations.0005_canonical_mobile_numbers")
        migration.forwards(django_apps, None)
        self.assertTrue(User.objects.filter(mobile_number="+919000000009").exists())


class MobileCollisionCommandTests(TestCase):
    def setUp(self):
        self.with_orders = _mobile_user("keep_me", "9000000005")
        Order.objects.create(user=self.with_orders, order_number="SMR-MOB-000001", subtotal=1, total=1)
        self.empty = _mobile_user("retire_me", "+919000000005")

    def _resolve(self, keep, retire, **options):
        call_command("resolve_mobile_collision", keep=str(keep.public_id), retire=str(retire.public_id),
                     stdout=StringIO(), **options)

    def test_audit_reports_masked_collision_and_fails_until_resolved(self):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("audit_mobile_numbers", stdout=out)
        text = out.getvalue()
        self.assertIn("Collisions: 1", text)
        self.assertIn("******0005", text)
        self.assertNotIn("9000000005", text)
        self.assertIn("orders 1", text)
        self.empty.refresh_from_db()
        self.assertEqual(self.empty.mobile_number, "+919000000005")  # read-only

    def test_resolve_dry_run_changes_nothing(self):
        self._resolve(self.with_orders, self.empty, dry_run=True)
        self.empty.refresh_from_db()
        self.assertEqual((self.empty.mobile_number, self.empty.is_active), ("+919000000005", True))

    def test_resolve_retires_empty_duplicate_then_audit_passes(self):
        self._resolve(self.with_orders, self.empty)
        self.empty.refresh_from_db()
        self.assertEqual((self.empty.mobile_number, self.empty.is_active), (None, False))
        self.assertEqual(Order.objects.get().user, self.with_orders)  # nothing moved or merged
        call_command("audit_mobile_numbers", stdout=StringIO())  # no collision left
        call_command("canonicalize_mobiles", stdout=StringIO())
        self.with_orders.refresh_from_db()
        self.assertEqual(self.with_orders.mobile_number, "+919000000005")

    def test_resolve_refuses_accounts_with_history(self):
        with self.assertRaisesMessage(CommandError, "history"):
            self._resolve(self.empty, self.with_orders)
        cart = Cart.objects.create(user=self.empty)
        cart.items.create(variant=_cart_variant(), quantity=1)
        with self.assertRaisesMessage(CommandError, "cart items"):
            self._resolve(self.with_orders, self.empty)
        self.empty.refresh_from_db()
        self.assertTrue(self.empty.is_active)

    def test_resolve_refuses_staff_and_different_numbers(self):
        other = _mobile_user("other", "9000000006")
        with self.assertRaisesMessage(CommandError, "same local number"):
            self._resolve(self.with_orders, other)
        User.objects.filter(pk=self.empty.pk).update(is_staff=True)
        with self.assertRaisesMessage(CommandError, "customer account"):
            self._resolve(self.with_orders, self.empty)


class MobileCollisionMergeOrdersTests(TestCase):
    """--merge-orders: the explicit business decision to move a duplicate's
    Orders to the kept account and retire the duplicate."""

    def setUp(self):
        from apps.ops.tests.test_ops import _order, _pay
        from apps.orders.models import OrderPayment, Refund, ShippingAddress

        self.OrderPayment, self.Refund, self.ShippingAddress = OrderPayment, Refund, ShippingAddress
        self.kept = _mobile_user("otp_user", "+919000000007")
        OTPVerification.objects.create(
            user=self.kept, otp_hash="x", expires_at=timezone.now(), is_verified=True,
        )
        self.retired = _mobile_user("bare_user", "9000000007")
        self.staff = User.objects.create_user(username="cashier", email="cashier@example.com", password="x",
                                              is_staff=True)
        self.orders = []
        for n in (71, 72):
            order = _order(n)
            Order.objects.filter(pk=order.pk).update(user=self.retired)
            self.orders.append(order)
        _pay(self.orders[0], "1000.00", self.staff)
        ShippingAddress.objects.create(
            order=self.orders[0], full_name="Asha", mobile="+919000000007", address_line1="1 MG Road",
            city="Pune", state="MH", pincode="411001",
        )
        returned = Return.objects.create(order=self.orders[1], status=Return.STATUS_COMPLETED)
        Refund.objects.create(return_request=returned, refund_amount=Decimal("10.00"),
                              refund_status=Refund.STATUS_COMPLETED, refund_method=Refund.METHOD_BANK_TRANSFER)

    def _resolve(self, keep=None, retire=None, **options):
        out = StringIO()
        call_command("resolve_mobile_collision", keep=str((keep or self.kept).public_id),
                     retire=str((retire or self.retired).public_id), stdout=out, **options)
        return out.getvalue()

    def _children(self):
        return (
            sorted(Order.objects.filter(pk__in=[o.pk for o in self.orders]).values_list("user_id", flat=True)),
            OrderItem.objects.filter(order__in=self.orders).count(),
            self.OrderPayment.objects.filter(order__in=self.orders).count(),
            Return.objects.filter(order__in=self.orders).count(),
            self.Refund.objects.filter(return_request__order__in=self.orders).count(),
            self.ShippingAddress.objects.filter(order__in=self.orders).count(),
        )

    def test_default_mode_still_refuses_when_both_have_history(self):
        with self.assertRaisesMessage(CommandError, "history"):
            self._resolve()
        with self.assertRaisesMessage(CommandError, "history"):
            self._resolve(keep=self.retired, retire=self.kept)

    def test_dry_run_reports_and_writes_nothing(self):
        before = self._children()
        text = self._resolve(merge_orders=True, dry_run=True)
        self.assertIn("Orders to move: 2", text)
        self.assertIn("Direct relations (retire): orders.Order.user=2", text)
        self.assertIn("accounts.OTPVerification.user=1", text)  # reported for the kept account
        self.assertIn("Blockers: none", text)
        self.assertIn("******0007", text)
        self.assertNotIn("9000000007", text)
        self.assertEqual(self._children(), before)
        self.retired.refresh_from_db()
        self.assertEqual((self.retired.mobile_number, self.retired.is_active), ("9000000007", True))

    def test_merge_moves_orders_and_keeps_their_children(self):
        _, items, payments, returns, refunds, addresses = self._children()
        with self.assertLogs("apps.accounts.management.commands.resolve_mobile_collision", level="INFO") as logs:
            text = self._resolve(merge_orders=True)
        self.assertEqual(self._children(), ([self.kept.pk, self.kept.pk], items, payments, returns, refunds,
                                            addresses))
        self.assertEqual((items, payments, returns, refunds, addresses), (2, 1, 1, 1, 1))
        self.retired.refresh_from_db()
        self.assertEqual((self.retired.mobile_number, self.retired.is_active), (None, False))
        self.kept.refresh_from_db()
        self.assertEqual((self.kept.mobile_number, self.kept.is_active), ("+919000000007", True))
        self.assertEqual(self.kept.otps.count(), 1)  # nothing else touched
        self.assertNotIn("9000000007", text + "".join(logs.output))
        self.assertIn("orders moved 2", "".join(logs.output))
        call_command("audit_mobile_numbers", stdout=StringIO())  # collision gone

    def test_unsupported_history_on_retired_account_refuses(self):
        OTPVerification.objects.create(user=self.retired, otp_hash="x", expires_at=timezone.now())
        before = self._children()
        with self.assertRaisesMessage(CommandError, "accounts.OTPVerification.user=1"):
            self._resolve(merge_orders=True)
        self.assertEqual(self._children(), before)
        User.objects.filter(pk=self.retired.pk).update(last_login=timezone.now())
        OTPVerification.objects.filter(user=self.retired).delete()
        with self.assertRaisesMessage(CommandError, "last login recorded"):
            self._resolve(merge_orders=True)

    def test_different_numbers_and_staff_refused(self):
        other = _mobile_user("other_num", "9000000009")
        with self.assertRaisesMessage(CommandError, "same local number"):
            self._resolve(retire=other, merge_orders=True)
        User.objects.filter(pk=self.retired.pk).update(is_superuser=True)
        with self.assertRaisesMessage(CommandError, "customer account"):
            self._resolve(merge_orders=True)
        User.objects.filter(pk=self.retired.pk).update(is_superuser=False)
        User.objects.filter(pk=self.kept.pk).update(is_staff=True)
        with self.assertRaisesMessage(CommandError, "active customer account"):
            self._resolve(merge_orders=True)

    def test_failure_rolls_back_everything(self):
        before = self._children()
        with patch.object(User, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._resolve(merge_orders=True)
        self.assertEqual(self._children(), before)  # orders not moved
        self.retired.refresh_from_db()
        self.assertEqual((self.retired.mobile_number, self.retired.is_active), ("9000000007", True))

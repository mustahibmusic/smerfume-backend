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

        self.guest_cart = Cart.objects.create(session_key="mergetoken")
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

        resp = self._do_otp_verify(self.user, "111111", cart_token="mergetoken")
        self.assertEqual(resp.status_code, 200)

        user_cart = Cart.objects.get(user=self.user)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().quantity, 3)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_guest_cart_deleted_after_merge(self, _mock):
        from apps.cart.models import Cart

        self._do_otp_verify(self.user, "222222", cart_token="mergetoken")
        self.assertFalse(Cart.objects.filter(session_key="mergetoken").exists())

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

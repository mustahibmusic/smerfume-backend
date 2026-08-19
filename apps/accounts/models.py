import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel


class User(AbstractUser):
    """Custom user model supporting email and mobile-number login."""

    ROLE_CHOICES = (
        ("customer", "Customer"),
        ("staff", "Staff"),
        ("admin", "Admin"),
    )

    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
    )
    email = models.EmailField(
        unique=True,
        error_messages={"unique": "A user with that email already exists."},
        null=True,
        blank=True,
    )
    mobile_number = models.CharField(
        max_length=15,
        unique=True,
        db_index=True,
        null=True,
        blank=True,
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default="customer",
        db_index=True,
    )

    # AbstractUser already provides:
    # password, last_login, is_superuser, username, first_name,
    # last_name, is_staff, is_active, date_joined

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    class Meta:
        db_table = "accounts_user"
        verbose_name = "user"
        verbose_name_plural = "users"

    @property
    def full_name(self) -> str:
        """Return first and last name joined, stripped of extra whitespace."""
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def role_label(self) -> str:
        """Return the human-readable label for the role."""
        return dict(self.ROLE_CHOICES).get(self.role, self.role)

    def __str__(self) -> str:
        return f"{self.role_label} - {self.email or self.mobile_number}"


class CustomerProfile(BaseModel):
    """Stores saved recipient / gift-contact details associated with a user.

    A user may accumulate multiple named contacts (themselves, family, etc.)
    for use at checkout. first_name is optional so the record can be created
    before the user has entered their details.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="customer_profile",
    )
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    mobile = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or str(self.user)


class OTPVerification(BaseModel):
    """Tracks a single OTP session for a user.

    Stores a bcrypt hash of the OTP instead of the plaintext code to limit
    exposure if the table is ever compromised. Each session has a unique
    otp_session_token so the client can reference it without exposing the
    user's identity.
    """

    PURPOSE_CHOICES = (
        ("login", "Login"),
    )

    # Class-level constants so callers can reference them without magic numbers
    MAX_ATTEMPTS = 3
    MAX_RESENDS = 3
    RESEND_COOLDOWN_SECONDS = 30
    OTP_EXPIRY_MINUTES = 5

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="otps",
    )
    otp_hash = models.CharField(max_length=128)
    otp_session_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        db_index=True,
    )
    purpose = models.CharField(
        max_length=20,
        choices=PURPOSE_CHOICES,
        default="login",
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    resend_count = models.PositiveSmallIntegerField(default=0)
    last_resend_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    is_new_user = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=self.OTP_EXPIRY_MINUTES)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        """True if the OTP has passed its expiry time."""
        return timezone.now() > self.expires_at

    @property
    def is_locked(self) -> bool:
        """True if the user has exhausted their allowed verification attempts."""
        return self.attempt_count >= self.MAX_ATTEMPTS

    @property
    def can_resend(self) -> bool:
        """True if the cooldown period since the last resend has elapsed."""
        if self.last_resend_at is None:
            return True
        elapsed = (timezone.now() - self.last_resend_at).total_seconds()
        return elapsed >= self.RESEND_COOLDOWN_SECONDS

    @property
    def resend_available_in(self) -> int:
        """Seconds remaining until a resend is allowed (0 if already allowed)."""
        if self.last_resend_at is None:
            return 0
        elapsed = (timezone.now() - self.last_resend_at).total_seconds()
        remaining = self.RESEND_COOLDOWN_SECONDS - elapsed
        return max(0, int(remaining))

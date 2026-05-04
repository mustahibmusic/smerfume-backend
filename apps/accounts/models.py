from django.contrib.auth.models import AbstractUser
from django.db import models
from apps.core.models import BaseModel
from django.utils import timezone
from datetime import timedelta
import random

class User(AbstractUser):
    ROLE_CHOICES = (
        ("customer", "Customer"),
        ("staff", "Staff"),
        ("admin", "Admin"),
    )

    # Email is our primary login ID
    email = models.EmailField(
        unique=True,
        error_messages={
            'unique': "A user with that email already exists.",
        },
        null=True,
        blank=True
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
    REQUIRED_FIELDS = ["username"]  # username is still technically required by AbstractUser

    class Meta:
        db_table = "accounts_user"  # Changed from auth_user to avoid conflicts
        verbose_name = "user"
        verbose_name_plural = "users"
    
    @property
    def role_label(self):
        """Returns the human-readable name for the role."""
        return dict(self.ROLE_CHOICES).get(self.role, self.role)

    def __str__(self):
        # Use the new property here instead
        return f"{self.role_label} - {self.email}"

class CustomerProfile(BaseModel):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="customer_profile"
    )
    # These can act as 'shipping' info if they differ from the User account
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    mobile = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"
    

class OTPVerification(BaseModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otps")
    code = models.CharField(max_length=6)
    is_verified = models.BooleanField(default=False)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=5)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at
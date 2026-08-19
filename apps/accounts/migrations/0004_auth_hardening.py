"""
Migration: AUTH-001 hardening changes.

Changes applied:
  1. Add public_id (nullable) to User
  2. Populate public_id for all existing User rows
  3. Make public_id non-nullable, unique, and indexed
  4. Delete all existing OTPVerification rows (they hold plaintext codes;
     the schema change makes them structurally invalid)
  5. Remove the plaintext 'code' field
  6. Add new secure fields: otp_hash, otp_session_token, purpose,
     attempt_count, resend_count, last_resend_at, is_new_user
  7. CustomerProfile.first_name — add blank=True
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


def populate_user_public_ids(apps, schema_editor):
    """Assign a unique UUID to every existing User row.

    Runs before the unique constraint is added so we never have a NULL
    or duplicate value when the constraint is enforced.
    """
    User = apps.get_model("accounts", "User")
    for user in User.objects.all():
        user.public_id = uuid.uuid4()
        user.save(update_fields=["public_id"])


def clear_otp_records(apps, schema_editor):
    """Delete all existing OTPVerification rows before the schema change.

    Existing rows store a plaintext OTP in the 'code' column.  The next
    steps remove that column and add otp_hash, making old rows structurally
    invalid and unverifiable.  It is safer to discard them than to leave
    orphaned, unverifiable records in the table.
    """
    OTPVerification = apps.get_model("accounts", "OTPVerification")
    OTPVerification.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_alter_user_email"),
    ]

    operations = [
        # 1. Add public_id as nullable first so existing rows are valid
        migrations.AddField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(null=True, blank=True),
        ),
        # 2. Populate all existing rows
        migrations.RunPython(
            populate_user_public_ids,
            reverse_code=migrations.RunPython.noop,
        ),
        # 3. Enforce unique + index now that every row has a value
        migrations.AlterField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
                db_index=True,
            ),
        ),
        # 4. Discard plaintext OTP rows before schema change
        migrations.RunPython(
            clear_otp_records,
            reverse_code=migrations.RunPython.noop,
        ),
        # 5. Remove plaintext code column
        migrations.RemoveField(
            model_name="otpverification",
            name="code",
        ),
        # 6a. Add otp_hash
        migrations.AddField(
            model_name="otpverification",
            name="otp_hash",
            field=models.CharField(max_length=128, default=""),
            preserve_default=False,
        ),
        # 6b. Add otp_session_token
        migrations.AddField(
            model_name="otpverification",
            name="otp_session_token",
            field=models.UUIDField(default=uuid.uuid4, unique=True, db_index=True),
        ),
        # 6c. Add purpose
        migrations.AddField(
            model_name="otpverification",
            name="purpose",
            field=models.CharField(
                choices=[("login", "Login")],
                default="login",
                max_length=20,
            ),
        ),
        # 6d. Add attempt_count
        migrations.AddField(
            model_name="otpverification",
            name="attempt_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        # 6e. Add resend_count
        migrations.AddField(
            model_name="otpverification",
            name="resend_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        # 6f. Add last_resend_at (nullable)
        migrations.AddField(
            model_name="otpverification",
            name="last_resend_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        # 6g. Add is_new_user
        migrations.AddField(
            model_name="otpverification",
            name="is_new_user",
            field=models.BooleanField(default=False),
        ),
        # 7. CustomerProfile.first_name — allow blank
        migrations.AlterField(
            model_name="customerprofile",
            name="first_name",
            field=models.CharField(max_length=100, blank=True),
        ),
    ]

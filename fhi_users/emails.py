from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from typing import TYPE_CHECKING
from fhi_users.models import VerificationToken
from fighthealthinsurance.utils import send_fallback_email
from django.utils.html import strip_tags
from urllib.parse import urlencode

if TYPE_CHECKING:
    from django.contrib.auth.models import User


def send_provider_started_appeal_email(patient_email, context):
    """Send email for provider started appeal."""
    send_fallback_email(
        "Provider Started Appeal",
        "provider_started_appeal",
        context,
        patient_email,
    )


def send_password_reset_email(user_email: str, token: str) -> None:
    """Send password reset email with secure URL construction."""
    subject = "Reset your password"
    params = urlencode({"token": token})
    reset_url = f"https://www.fightpaperwork.com/reset-password/finish?{params}"
    send_fallback_email(
        subject,
        "password_reset",
        {"reset_url": reset_url},
        user_email,
    )


def send_email_confirmation(user_email, context):
    """Send email confirmation email."""
    send_fallback_email("Email Confirmation", "email_confirmation", context, user_email)


def send_appeal_submitted_successfully_email(user_email, context):
    """Send appeal submission success email."""
    send_fallback_email(
        "Appeal Submitted Successfully",
        "appeal_submitted_successfully",
        context,
        user_email,
    )


def send_error_submitting_appeal_email(user_email, context):
    """Send error submitting appeal email."""
    send_fallback_email(
        "Error Submitting Appeal",
        "error_submitting_appeal",
        context,
        user_email,
    )


def send_verification_email(request, user: "User") -> None:
    """Send verification email with secure activation link."""
    current_site = get_current_site(request)
    mail_subject = "Activate your account."
    verification_token = default_token_generator.make_token(user)
    VerificationToken.objects.create(user=user, token=verification_token)
    params = urlencode({"token": verification_token, "uid": user.pk})
    activation_link = f"https://www.fightpaperwork.com/activate-account/?{params}"
    send_fallback_email(
        mail_subject,
        "acc_active_email",
        {"user": user, "domain": current_site.domain, "activation_link": activation_link},
        user.email,
    )

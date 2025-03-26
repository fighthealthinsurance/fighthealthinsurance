from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from typing import TYPE_CHECKING, Optional
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
    reset_link = (
        f"https://www.fightpaperwork.com/auth/reset-password/new-password?{params}"
    )
    send_fallback_email(
        subject,
        "password_reset",
        {"reset_link": reset_link},
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
        {
            "user": user,
            "domain": current_site.domain,
            "activation_link": activation_link,
        },
        user.email,
    )


def send_checkout_session_expired(
    request, email: str, link: str, item: Optional[str]
) -> None:
    """Send checkout session expired email."""
    default_item = "Fight Health Insurance / Fight Paperwork"
    item = item if item else default_item
    send_fallback_email(
        f"{item} Checkout Session Expired",
        "checkout_session_expired",
        {
            "link": link,
        },
        email,
    )


def send_professional_invitation_email(professional_email, context):
    """Send invitation email to a professional to join a practice."""
    send_fallback_email(
        "Invitation to Join Practice",
        "invite_professional",
        context,
        professional_email,
    )


def send_provider_created_email(professional_email, context):
    """Send email to a professional that was created by an admin."""
    send_fallback_email(
        "Your Professional Account Has Been Created",
        "provider_created",
        context,
        professional_email,
    )

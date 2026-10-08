from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from typing import TYPE_CHECKING, Optional
from fhi_users.models import VerificationToken
from fighthealthinsurance.utils import send_fallback_email
from django.utils import timezone
from datetime import timedelta
from urllib.parse import urlencode
from loguru import logger

if TYPE_CHECKING:
    from django.contrib.auth.models import User


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


def send_verification_email(request, user: "User", first_only: bool = False) -> None:
    """Send verification email with secure activation link."""
    current_site = get_current_site(request)
    # Check if there is an existing token
    if VerificationToken.objects.filter(user=user).exists():
        if first_only:
            logger.debug(f"Skipping verification e-mail to {user} as already sent")
            return
        else:
            current_token = VerificationToken.objects.filter(user=user).first()
            if current_token and current_token.created_at > timezone.now() - timedelta(
                minutes=10
            ):
                logger.debug(
                    f"Skipping verification e-mail to {user} as already sent within 10 minutes"
                )
                return
            VerificationToken.objects.filter(user=user).delete()
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
    request,
    email: str,
    link: str,
    item: Optional[str],
    payment_type: Optional[str] = None,
) -> None:
    """Email the link back to a checkout that ran out. A fax started
    sending when it was staged, before its checkout opened, so that email
    says paying is optional rather than asking for it."""
    fax = payment_type == "fax"
    if fax:
        subject = "Paying for your appeal fax is optional"
    else:
        subject = f"Your {item or 'Fight Health Insurance'} checkout didn't finish"
    send_fallback_email(
        subject,
        "checkout_session_expired",
        {"link": link, "fax": fax},
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


def send_professional_created_email(professional_email, context):
    """Send email to a professional that was created by an admin."""
    send_fallback_email(
        "Your Professional Account Has Been Created",
        "professional_created",
        context,
        professional_email,
    )

from django.core.mail import send_mail
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from typing import TYPE_CHECKING
from fhi_users.models import VerificationToken
from django.utils.html import strip_tags

if TYPE_CHECKING:
    from django.contrib.auth.models import User


def send_fallback_email(subject, template_name, context, to_email):
    # First, render the plain text content if present
    text_content = render_to_string(
        f"emails/{template_name}.txt",
        context=context,
    )

    # Secondly, render the HTML content if present
    html_content = render_to_string(
        f"emails/{template_name}.html",
        context=context,
    )
    # Then, create a multipart email instance.
    msg = EmailMultiAlternatives(
        subject,
        text_content,
        settings.EMAIL_HOST_USER,
        [to_email],
    )

    # Lastly, attach the HTML content to the email instance and send.
    msg.attach_alternative(html_content, "text/html")
    msg.send()


def send_provider_started_appeal_email(patient_email, context):
    send_fallback_email(
        "Provider Started Appeal",
        "provider_started_appeal",
        context,
        patient_email,
    )


def send_password_reset_email(user_email: str, token: str) -> None:
    """
    Send password reset email with reset token.
    """
    subject = "Reset your password"
    send_fallback_email(
        subject,
        "password_reset",
        {
            "reset_url": f"https://www.fightpaperwork.com/reset-password/finish?token={token}"
        },
        user_email,
    )


def send_email_confirmation(user_email, context):
    send_fallback_email("Email Confirmation", "email_confirmation", context, user_email)


def send_appeal_submitted_successfully_email(user_email, context):
    send_fallback_email(
        "Appeal Submitted Successfully",
        "appeal_submitted_successfully",
        context,
        user_email,
    )


def send_error_submitting_appeal_email(user_email, context):
    send_fallback_email(
        "Error Submitting Appeal",
        "error_submitting_appeal",
        context,
        user_email,
    )


def send_verification_email(request, user: "User") -> None:
    current_site = get_current_site(request)
    mail_subject = "Activate your account."
    verification_token = default_token_generator.make_token(user)
    VerificationToken.objects.create(user=user, token=verification_token)
    send_fallback_email(
        mail_subject,
        "acc_active_email",
        {
            "user": user,
            "domain": current_site.domain,
            "uid": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": verification_token,
        },
        user.email,
    )

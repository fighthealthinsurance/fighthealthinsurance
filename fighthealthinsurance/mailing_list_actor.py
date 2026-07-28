import os
import smtplib
import time
from typing import Iterable, List, Optional, Tuple

import ray
from loguru import logger

from fighthealthinsurance.utils import get_env_variable

name = "MailingListActor"

# (email address, unsubscribe URL or None) pairs handed to the send loop.
Recipient = Tuple[str, Optional[str]]


def dedupe_recipients_by_email(recipients: Iterable[Recipient]) -> List[Recipient]:
    """Collapse recipients sharing an email address, keeping the first seen.

    Both audiences can hold several rows for one person -- duplicate
    professional signups, or a subscriber who signed up twice -- and a
    broadcast must reach each address exactly once. Matching is
    case-insensitive because addresses are stored as typed.
    """
    seen: set[str] = set()
    out: List[Recipient] = []
    for email, unsubscribe_url in recipients:
        if not email:
            continue
        key = email.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((email, unsubscribe_url))
    return out


@ray.remote(max_restarts=-1, max_task_retries=-1)
class MailingListActor:
    """Ray actor for sending bulk (mailing list / professional) emails asynchronously."""

    def __init__(self) -> None:
        logger.info("Starting MailingListActor")
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        get_wsgi_application()
        logger.info("MailingListActor wsgi started")

    def hi(self) -> str:
        return "ok"

    def send_mailing_list_email(
        self,
        subject: str,
        html_content: str,
        text_content: str,
        test_email: Optional[str] = None,
    ) -> Tuple[int, int, int]:
        """
        Send mailing list email to all subscribers or a test email.

        Args:
            subject: Email subject
            html_content: HTML version of the email
            text_content: Plain text version of the email
            test_email: Optional test email address. If provided, only sends to this address.

        Returns:
            Tuple of (sent_count, failed_count, blocked_count)
        """
        from fighthealthinsurance.models import MailingListSubscriber

        if test_email:
            recipients = self._test_recipients(test_email)
        else:
            recipients = dedupe_recipients_by_email(
                (sub.email, sub.get_unsubscribe_url())
                for sub in MailingListSubscriber.objects.all().iterator()
            )
        return self._send_bulk(
            subject, html_content, text_content, recipients, audience="mailing list"
        )

    def send_interested_professional_email(
        self,
        subject: str,
        html_content: str,
        text_content: str,
        test_email: Optional[str] = None,
    ) -> Tuple[int, int, int]:
        """
        Send an email to all interested professionals or a test email.

        Recipients come from ``mailable_interested_professionals`` -- every
        professional signup except test/spam records and anyone who has opted
        out -- deduped by address so duplicate signups are mailed once.

        Args:
            subject: Email subject
            html_content: HTML version of the email
            text_content: Plain text version of the email
            test_email: Optional test email address. If provided, only sends to this address.

        Returns:
            Tuple of (sent_count, failed_count, blocked_count)
        """
        from fighthealthinsurance.proconnector import (
            mailable_interested_professionals,
        )

        if test_email:
            recipients = self._test_recipients(test_email)
        else:
            recipients = dedupe_recipients_by_email(
                (pro.email, pro.get_unsubscribe_url())
                for pro in mailable_interested_professionals().iterator()
            )
        return self._send_bulk(
            subject,
            html_content,
            text_content,
            recipients,
            audience="interested professional",
        )

    @staticmethod
    def _test_recipients(test_email: str) -> List[Recipient]:
        """Recipient list for a test send: the one address, no unsubscribe link.

        A test copy goes to a staff address that is not on the list, so there is
        no token to build a working unsubscribe URL from.
        """
        return [(test_email, None)]

    def _send_bulk(
        self,
        subject: str,
        html_content: str,
        text_content: str,
        recipients: List[Recipient],
        audience: str,
    ) -> Tuple[int, int, int]:
        """Send one message to every recipient over a single SMTP connection.

        Shared by both audiences: same blocked-address filtering, same
        unsubscribe-footer handling, same per-recipient error isolation (one bad
        address never aborts the run). ``audience`` only labels log lines.

        Returns (sent_count, failed_count, blocked_count).
        """
        from django.conf import settings
        from django.core.mail import EmailMultiAlternatives, get_connection

        from fighthealthinsurance.email_utils import is_blocked_email
        from fighthealthinsurance.utils import mask_email_for_logging

        sent_count = 0
        failed_count = 0
        blocked_count = 0

        # Use connection reuse for better performance
        connection = get_connection()

        try:
            connection.open()
            for email, unsubscribe_url in recipients:
                if is_blocked_email(email):
                    blocked_count += 1
                    masked_email = mask_email_for_logging(email)
                    logger.info(
                        f"Skipping blocked email in {audience} send: {masked_email}"
                    )
                    continue
                try:
                    # Append unsubscribe link to content if available
                    if unsubscribe_url:
                        final_html = self._append_unsubscribe_html(
                            html_content, unsubscribe_url
                        )
                        final_text = self._append_unsubscribe_text(
                            text_content, unsubscribe_url
                        )
                    else:
                        final_html = html_content
                        final_text = text_content

                    msg = EmailMultiAlternatives(
                        subject,
                        final_text,
                        settings.DEFAULT_FROM_EMAIL,
                        to=[email],
                        connection=connection,
                    )
                    msg.attach_alternative(final_html, "text/html")
                    msg.send()
                    sent_count += 1
                    masked_email = mask_email_for_logging(email)
                    logger.info(f"Sent {audience} email to {masked_email}")
                except smtplib.SMTPException as e:
                    failed_count += 1
                    masked_email = mask_email_for_logging(email)
                    logger.error(
                        f"SMTP error sending {audience} email to {masked_email}: {e}"
                    )
                except OSError as e:
                    failed_count += 1
                    masked_email = mask_email_for_logging(email)
                    logger.error(
                        f"Connection error sending {audience} email to {masked_email}: {e}"
                    )
        finally:
            connection.close()

        return (sent_count, failed_count, blocked_count)

    def _append_unsubscribe_html(self, html_content: str, unsubscribe_url: str) -> str:
        """Append unsubscribe link to HTML content."""
        unsubscribe_html = (
            f'<br><hr><p style="font-size: 12px; color: #666;">'
            f"To unsubscribe from this mailing list, "
            f'<a href="{unsubscribe_url}">click here</a>.</p>'
        )
        return html_content + unsubscribe_html

    def _append_unsubscribe_text(self, text_content: str, unsubscribe_url: str) -> str:
        """Append unsubscribe link to text content."""
        unsubscribe_text = (
            f"\n\n---\n"
            f"To unsubscribe from this mailing list, visit: {unsubscribe_url}"
        )
        return text_content + unsubscribe_text

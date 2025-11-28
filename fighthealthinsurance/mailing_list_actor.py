import os
import smtplib
from typing import Optional, Tuple

import ray
import time

from fighthealthinsurance.utils import get_env_variable
from loguru import logger

name = "MailingListActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class MailingListActor:
    """Ray actor for sending mailing list emails asynchronously."""

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
    ) -> Tuple[int, int]:
        """
        Send mailing list email to all subscribers or a test email.

        Args:
            subject: Email subject
            html_content: HTML version of the email
            text_content: Plain text version of the email
            test_email: Optional test email address. If provided, only sends to this address.

        Returns:
            Tuple of (sent_count, failed_count)
        """
        from django.conf import settings
        from django.core.mail import EmailMultiAlternatives, get_connection

        from fighthealthinsurance.models import MailingListSubscriber
        from fighthealthinsurance.utils import mask_email_for_logging

        sent_count = 0
        failed_count = 0

        if test_email:
            # For test email, we don't include unsubscribe link
            recipients: list[tuple[str, Optional[str]]] = [(test_email, None)]
        else:
            # Get all subscribers with their unsubscribe URLs
            subscribers = MailingListSubscriber.objects.all().iterator()
            recipients = [
                (sub.email, sub.get_unsubscribe_url()) for sub in subscribers
            ]

        # Use connection reuse for better performance
        connection = get_connection()
        try:
            connection.open()
            for email, unsubscribe_url in recipients:
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
                    logger.info(f"Sent mailing list email to {masked_email}")
                except smtplib.SMTPException as e:
                    failed_count += 1
                    masked_email = mask_email_for_logging(email)
                    logger.error(
                        f"SMTP error sending mailing list email to {masked_email}: {e}"
                    )
                except OSError as e:
                    failed_count += 1
                    masked_email = mask_email_for_logging(email)
                    logger.error(
                        f"Connection error sending mailing list email to {masked_email}: {e}"
                    )
        finally:
            connection.close()

        return (sent_count, failed_count)

    def _append_unsubscribe_html(self, html_content: str, unsubscribe_url: str) -> str:
        """Append unsubscribe link to HTML content."""
        unsubscribe_html = (
            f'<br><hr><p style="font-size: 12px; color: #666;">'
            f'To unsubscribe from this mailing list, '
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

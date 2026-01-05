"""Email sender for calendar reminders.

This module handles sending calendar-related emails including:
1. Initial calendar email with .ics attachment (sent immediately after appeal generation)
2. Scheduled reminder emails at 2/30/90 day intervals
"""

import datetime
from typing import Any, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.utils import NotSupportedError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.calendar_utils import generate_followup_ics
from fighthealthinsurance.models import CalendarReminder


def _get_calendar_download_url(denial: Any) -> str:
    """Generate calendar download URL for a denial."""
    return "https://www.fighthealthinsurance.com" + reverse(
        "download_calendar",
        kwargs={
            "denial_uuid": denial.uuid,
            "hashed_email": denial.hashed_email,
        },
    )


def send_calendar_email_with_attachment(
    subject: str,
    template_name: str,
    context: dict,
    to_email: str,
    ics_content: bytes,
    ics_filename: str = "followup_reminders.ics",
) -> bool:
    """Send an email with an .ics calendar file attachment.

    Args:
        subject: Email subject line
        template_name: Name of email template (without .html/.txt extension)
        context: Template context dictionary
        to_email: Recipient email address
        ics_content: iCalendar file content as bytes
        ics_filename: Filename for the .ics attachment

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    if to_email.endswith("-fake@fighthealthinsurance.com"):
        return False

    try:
        # Render plain text content
        text_content = render_to_string(
            f"emails/{template_name}.txt",
            context=context,
        )

        # Render HTML content
        html_content = render_to_string(
            f"emails/{template_name}.html",
            context=context,
        )

        # Create multipart email
        msg = EmailMultiAlternatives(
            subject,
            text_content,
            settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
        )

        logger.debug(f"Sending calendar email to {to_email} with subject {subject}")

        # Attach HTML content
        msg.attach_alternative(html_content, "text/html")

        # Attach .ics file
        msg.attach(ics_filename, ics_content, "text/calendar")

        # Send email
        msg.send()

        # Also send copy to support for monitoring (like send_fallback_email does)
        try:
            second_msg = EmailMultiAlternatives(
                subject + " -- " + to_email,
                text_content,
                settings.DEFAULT_FROM_EMAIL,
                to=["support42@fighthealthinsurance.com"],
            )
            second_msg.attach_alternative(html_content, "text/html")
            second_msg.attach(ics_filename, ics_content, "text/calendar")
            second_msg.send()
        except Exception as e:
            logger.debug(f"Failed to send copy to support: {str(e)}")

        return True

    except Exception as e:
        logger.debug(f"Failed to send calendar email to {to_email}: {str(e)}")
        return False


def send_initial_calendar_email(denial: Any, email: str) -> bool:
    """Send initial calendar email with .ics attachment immediately after appeal generation.

    This email includes:
    - Welcome message about calendar reminders
    - .ics file attachment with 3 events (2/30/90 days)
    - Instructions to mark emails as NOT SPAM
    - Disclaimer that reminders are helpful but imperfect

    Args:
        denial: Denial model instance
        email: Email address to send to

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    try:
        # Generate .ics file
        ics_content = generate_followup_ics(denial, email)

        # Get insurance company name
        insurance_company = getattr(denial, "insurance_company", "your insurance")

        # Create email context
        context = {
            "email": email,
            "insurance_company": insurance_company,
            "calendar_download_url": _get_calendar_download_url(denial),
            "denial": denial,
        }

        # Send email with attachment
        success = send_calendar_email_with_attachment(
            subject=f"Calendar Reminders for {insurance_company} Appeal - Fight Health Insurance",
            template_name="calendar_initial",
            context=context,
            to_email=email,
            ics_content=ics_content,
            ics_filename=f"fhi_appeal_reminders_{denial.uuid}.ics",
        )

        log_level = logger.info if success else logger.warning
        log_level(
            f"{'Successfully sent' if success else 'Failed to send'} initial calendar email to {email} for denial {denial.uuid}"
        )
        return success

    except Exception as e:
        logger.error(
            f"Error sending initial calendar email to {email} for denial {denial.uuid}: {str(e)}"
        )
        return False


class CalendarReminderSender:
    """Sends scheduled calendar reminder emails at 2/30/90 day intervals.

    This class is used by the send_calendar_reminders management command to
    find and send reminder emails to users who have calendar reminders scheduled
    for today or earlier.

    Pattern follows FollowUpEmailSender for consistency.
    """

    def _find_candidates(self):
        """Find calendar reminders that are due to be sent.

        Returns:
            QuerySet: CalendarReminder objects that need to be sent
        """
        try:
            # Find reminders that haven't been sent and are due (or overdue)
            candidates = (
                CalendarReminder.objects.filter(reminder_sent=False)
                .filter(reminder_date__lte=datetime.date.today())
                .select_related("denial")
                .distinct("email")
            )
            # Force partial evaluation to catch DISTINCT ON errors
            # Used for SQLite in local dev/test mode
            candidates.exists()
            return candidates
        except NotSupportedError:
            # Fallback for databases that don't support DISTINCT ON
            candidates = (
                CalendarReminder.objects.filter(reminder_sent=False)
                .filter(reminder_date__lte=datetime.date.today())
                .select_related("denial")
            )
            return candidates

    def find_candidates(self) -> list[CalendarReminder]:
        """Find and return list of calendar reminders to send.

        Returns:
            list[CalendarReminder]: List of reminder objects
        """
        return list(self._find_candidates())

    def send_all(self, count: Optional[int] = None) -> int:
        """Send all pending calendar reminder emails.

        Args:
            count: Optional limit on number of emails to send

        Returns:
            int: Number of emails successfully sent
        """
        candidates = self.find_candidates()[:count] if count else self.find_candidates()
        sent_count = 0
        for reminder in candidates:
            if self.dosend(calendar_reminder=reminder):
                sent_count += 1
        return sent_count

    def dosend(
        self,
        calendar_reminder: Optional[CalendarReminder] = None,
        email: Optional[str] = None,
    ) -> bool:
        """Send a single calendar reminder email.

        Args:
            calendar_reminder: CalendarReminder object to send
            email: Alternative way to specify reminder by email (finds first unsent)

        Returns:
            bool: True if email sent successfully, False otherwise
        """
        if calendar_reminder is None and email is not None:
            calendar_reminder = (
                CalendarReminder.objects.filter(email=email)
                .filter(reminder_sent=False)
                .first()
            )
        elif calendar_reminder is None and email is None:
            raise Exception("One of email and calendar_reminder must be set.")

        # At this point calendar_reminder should be set
        assert calendar_reminder is not None

        email = calendar_reminder.email
        denial = calendar_reminder.denial
        reminder_type = calendar_reminder.get_reminder_type_display()
        insurance_company = getattr(denial, "insurance_company", "your insurance")

        # Create email context
        context = {
            "email": email,
            "insurance_company": insurance_company,
            "calendar_download_url": _get_calendar_download_url(denial),
            "denial": denial,
            "reminder_type": reminder_type,
            "selected_appeal": (
                denial.chose_appeal() if hasattr(denial, "chose_appeal") else None
            ),
            "reminder_type_code": calendar_reminder.reminder_type,
        }

        try:
            # Render plain text content
            from fighthealthinsurance.utils import send_fallback_email

            send_fallback_email(
                template_name="calendar_reminder",
                subject=f"Reminder: Follow up on {insurance_company} appeal ({reminder_type}) - Fight Health Insurance",
                context=context,
                to_email=email,
            )

            # Mark as sent
            calendar_reminder.reminder_sent = True
            calendar_reminder.reminder_sent_date = timezone.now()
            calendar_reminder.save()

            logger.info(
                f"Successfully sent {reminder_type} calendar reminder to {email} for denial {denial.uuid}"
            )
            return True

        except Exception as e:
            logger.debug(
                f"Failed to send calendar reminder to {email} for denial {denial.uuid}: {str(e)}"
            )
            return False

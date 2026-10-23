"""The share page's opt-in reminder: one email, 30 days after asking, to share
the plan documents a plan has sent by then.

The address is kept only until the email goes. Sending claims a reminder by
deleting its row first, so there is at most one email and the address is
gone whatever happens to the send; a failed send is not retried. The email
goes to the person alone, with no staff copy. EmailPollingActor sends what is
due on its regular pass, and remove-my-data deletes a reminder still waiting.
"""

import datetime
from typing import Any, Optional

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.email_utils import is_blocked_email
from fighthealthinsurance.followup_emails import AsyncEmailSenderMixin
from fighthealthinsurance.models import PlanDocumentsReminder, SharedPlanDocumentsDay
from fighthealthinsurance.utils import build_fallback_email

REMINDER_DAYS = 30
SUBJECT = "A reminder to share your plan documents"
TEMPLATE = "plan_documents_reminder"

CANT_SEND_MESSAGE = "We can't send email to that address. Please check it."
DAILY_LIMIT_MESSAGE = (
    "We've had a lot of reminder requests today. Please try again tomorrow."
)


def _now() -> datetime.datetime:
    return timezone.now()


def _requests_per_day() -> int:
    return int(getattr(settings, "PLAN_DOCUMENTS_REMINDERS_DAILY", 200))


def schedule_reminder(email: str) -> Optional[str]:
    """Set the reminder for ``email``. Returns None when it is set, else what
    to tell the person. Asking again moves the date; it never adds a second
    email."""
    from fighthealthinsurance.shared_plan_documents import reserve

    email = (email or "").strip().lower()
    if is_blocked_email(email):
        return CANT_SEND_MESSAGE
    now = _now()
    waiting = PlanDocumentsReminder.objects.filter(email=email).exists()
    if not waiting and (
        reserve(SharedPlanDocumentsDay.REMINDERS, 1, _requests_per_day()) is None
    ):
        return DAILY_LIMIT_MESSAGE
    PlanDocumentsReminder.objects.update_or_create(
        email=email,
        defaults={"send_after": now + datetime.timedelta(days=REMINDER_DAYS)},
    )
    logger.info("Set a plan documents reminder")
    return None


def share_page_url() -> str:
    base = getattr(
        settings, "FHI_PUBLIC_BASE_URL", "https://www.fighthealthinsurance.com"
    )
    return base.rstrip("/") + reverse("share_plan_documents")


class PlanDocumentsReminderSender(AsyncEmailSenderMixin):
    """Sends the reminders that are due, oldest first."""

    def find_candidates(self) -> list[int]:
        return list(
            PlanDocumentsReminder.objects.filter(send_after__lte=_now())
            .order_by("send_after")
            .values_list("pk", flat=True)[:100]
        )

    def dosend(self, reminder_id: int) -> bool:
        now = _now()
        due = PlanDocumentsReminder.objects.filter(pk=reminder_id, send_after__lte=now)
        email = due.values_list("email", flat=True).first()
        if email is None:
            return False
        # The claim: whoever deletes the row sends, once, and the address goes.
        deleted, _ = due.delete()
        if not deleted or is_blocked_email(email):
            return False
        try:
            build_fallback_email(
                SUBJECT, TEMPLATE, {"url": share_page_url()}, email
            ).send()
        except Exception as error:
            # Only the error's type: a mail server's message can carry the address.
            logger.error(
                f"A plan documents reminder was not sent: {type(error).__name__}"
            )
            return False
        logger.info("Sent a plan documents reminder")
        return True

    async def _asend_one(self, candidate: Any) -> bool:
        return await self.adosend(reminder_id=candidate)

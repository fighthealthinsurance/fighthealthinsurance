"""The share page's opt-in reminder: one email, 30 days after asking, to share
the plan documents a plan has sent by then.

The address is kept only until the email goes. Sending claims a reminder by
deleting its row first, so there is at most one email and the address is
gone whatever happens to the send; a failed send is not retried. The address
is read only from a row the sender deleted, and a failed claim or send logs
only the error's type. The email goes to the person alone, with no staff
copy. The hourly plan documents sweep on Temporal sends what is due when
temporal_client.plan_documents_enabled() is true, and EmailPollingActor on
its regular pass when it is not. Remove-my-data deletes a reminder still
waiting.
"""

import datetime
from typing import Any, Optional

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
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
    email. Every request counts against the day's budget, so the answer
    never shows whether an address already asked."""
    from fighthealthinsurance.shared_plan_documents import give_back, reserve

    email = (email or "").strip().lower()
    if is_blocked_email(email):
        return CANT_SEND_MESSAGE
    now = _now()
    day = reserve(SharedPlanDocumentsDay.REMINDERS, 1, _requests_per_day())
    if day is None:
        return DAILY_LIMIT_MESSAGE
    try:
        PlanDocumentsReminder.objects.update_or_create(
            email=email,
            defaults={"send_after": now + datetime.timedelta(days=REMINDER_DAYS)},
        )
    except Exception:
        give_back(SharedPlanDocumentsDay.REMINDERS, 1, day)
        raise
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

    def claim(self, reminder_id: int) -> Optional[str]:
        """Delete a due reminder's row and return its address, or None when
        there is nothing to send: not due, claimed already, or an address we
        never send to. Whoever deletes the row sends, once, and the address
        goes."""
        try:
            claimed = PlanDocumentsReminder.objects.filter(
                pk=reminder_id, send_after__lte=_now()
            ).first()
            if claimed is None:
                return None
            # Still due: a reminder asked for again meanwhile has moved on.
            deleted, _ = PlanDocumentsReminder.objects.filter(
                pk=claimed.pk, send_after__lte=_now()
            ).delete()
        except Exception as error:
            # Only the error's type: the row holds an address.
            logger.error(
                f"A plan documents reminder was not claimed: {type(error).__name__}"
            )
            return None
        if not deleted:
            return None
        # Read only from the row this sender deleted.
        email = claimed.email
        if is_blocked_email(email):
            return None
        return email

    def send_to(self, email: str) -> bool:
        """Send the reminder to an address claim returned. Never retried."""
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

    def dosend(self, reminder_id: int) -> bool:
        email = self.claim(reminder_id)
        return email is not None and self.send_to(email)

    async def _asend_one(self, candidate: Any) -> bool:
        return await self.adosend(reminder_id=candidate)


async def adue_reminder_ids(limit: int) -> list[int]:
    """The ids of up to ``limit`` reminders that are due, oldest first."""
    due = await PlanDocumentsReminderSender().afind_candidates()
    return [int(pk) for pk in due[:limit]]


async def asend_reminder(reminder_id: int) -> bool:
    """Claim one reminder and send it, for the plan documents sweep. The
    address is read from the claimed row here and goes no further."""
    sender = PlanDocumentsReminderSender()
    email = await database_sync_to_async(sender.claim)(reminder_id)
    if email is None:
        return False
    # Mail only, no ORM: off the database thread, so a slow mail server
    # holds up no other activity's queries.
    return bool(await sync_to_async(sender.send_to, thread_sensitive=False)(email))

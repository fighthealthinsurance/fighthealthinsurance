"""Queue + sender for emails deferred to the recipient's likely business hours.

``enqueue_scheduled_email`` records a templated email plus the timezone window
that gates it (see ``business_hours.py``). ``ScheduledEmailSender`` is driven by
``EmailPollingActor`` to deliver due emails once their window is open, mirroring
the existing ``FollowUpEmailSender`` pattern.
"""

import datetime
from typing import Any, Optional

from django.utils import timezone

from loguru import logger

from fighthealthinsurance.business_hours import (
    is_within_business_hours,
    next_business_hours_start,
    resolve_send_timezone,
)
from fighthealthinsurance.followup_emails import AsyncEmailSenderMixin
from fighthealthinsurance.models import ScheduledEmail
from fighthealthinsurance.utils import mask_email_for_logging, send_fallback_email

# Retry backoff for a scheduled email whose send raised: exponential per attempt
# (1h, 2h, 4h, ... capped) so a permanently-broken row doesn't churn the queue
# at a fixed interval until it gives up.
_INITIAL_SEND_RETRY_BACKOFF = datetime.timedelta(hours=1)
_MAX_SEND_RETRY_BACKOFF = datetime.timedelta(hours=24)
# Stop retrying after this many failed attempts so a permanently-broken row
# doesn't churn the queue forever.
MAX_SEND_ATTEMPTS = 8
# Soft-claim window: when a poller picks up a due row it pushes ``send_after``
# this far out *before* the external send, so a second concurrent poller won't
# also grab it. If the sender dies mid-send the row simply becomes eligible
# again after this hour rather than being lost or double-sent.
_SOFT_CLAIM_TTL = datetime.timedelta(hours=1)


def enqueue_scheduled_email(
    *,
    to_email: str,
    subject: str,
    template_name: str,
    context: Optional[dict] = None,
    cc: Optional[list[str]] = None,
    phone: Optional[str] = None,
    purpose: str = "",
) -> ScheduledEmail:
    """Queue a templated email to be sent during the recipient's business hours.

    The recipient's timezone is inferred from ``phone`` when possible (otherwise
    the conservative cross-US Pacific overlap is used), and ``send_after`` is set
    to the next opening of that window.
    """
    tz_name, is_specific = resolve_send_timezone(phone)
    send_after = next_business_hours_start(tz_name, is_specific)
    return ScheduledEmail.objects.create(
        to_email=to_email,
        subject=subject,
        template_name=template_name,
        context=context or {},
        cc=list(cc or []),
        send_after=send_after,
        send_timezone=tz_name,
        timezone_is_specific=is_specific,
        purpose=purpose,
    )


class ScheduledEmailSender(AsyncEmailSenderMixin):
    """Delivers queued ``ScheduledEmail`` rows once their window is open."""

    def find_candidates(self) -> list[ScheduledEmail]:
        """Unsent, not-given-up emails whose ``send_after`` has passed, oldest first."""
        now = timezone.now()
        return list(
            ScheduledEmail.objects.filter(
                sent=False,
                send_after__lte=now,
                attempts__lt=MAX_SEND_ATTEMPTS,
            ).order_by("send_after")[:100]
        )

    def dosend(
        self,
        scheduled_email: Optional[ScheduledEmail] = None,
        scheduled_email_id: Optional[int] = None,
    ) -> bool:
        """Send one queued email if its window is open; otherwise defer it.

        Returns ``True`` only when an email is actually sent. A row whose window
        has since closed is rescheduled to the next opening (returns ``False``),
        and a send failure is backed off and recorded (returns ``False``).
        """
        if scheduled_email is None and scheduled_email_id is not None:
            scheduled_email = ScheduledEmail.objects.filter(
                pk=scheduled_email_id
            ).first()
        if scheduled_email is None:
            return False
        se = scheduled_email
        if se.sent:
            return False

        now = timezone.now()
        # Re-check the live window: a row can become due (send_after passed) but
        # only after its window already closed for the day -> wait for next open.
        if not is_within_business_hours(now, se.send_timezone, se.timezone_is_specific):
            se.send_after = next_business_hours_start(
                se.send_timezone, se.timezone_is_specific, now=now
            )
            se.save(update_fields=["send_after"])
            return False

        # Atomically soft-claim the row before the external send so two
        # concurrent pollers can't both deliver it: push ``send_after`` an hour
        # out, conditional on the row still being due and unsent. Only the first
        # claimant matches (others now see a future ``send_after`` and skip); a
        # crash mid-send just lets the row become eligible again after the claim
        # window instead of double-sending.
        claimed = ScheduledEmail.objects.filter(
            pk=se.pk,
            sent=False,
            send_after__lte=now,
            attempts__lt=MAX_SEND_ATTEMPTS,
        ).update(send_after=now + _SOFT_CLAIM_TTL)
        if claimed == 0:
            return False
        se.refresh_from_db()

        try:
            send_fallback_email(
                subject=se.subject,
                template_name=se.template_name,
                context=se.context or {},
                to_email=se.to_email,
                cc=se.cc or None,
            )
        except Exception as e:
            # Exponential backoff keyed on attempts already made (1h, 2h, 4h ...
            # capped) so a broken template/backend stops churning the queue.
            retry_backoff = min(
                _INITIAL_SEND_RETRY_BACKOFF * (2**se.attempts),
                _MAX_SEND_RETRY_BACKOFF,
            )
            se.attempts = se.attempts + 1
            se.last_error = str(e)[:2000]
            se.send_after = now + retry_backoff
            se.save(update_fields=["attempts", "last_error", "send_after"])
            logger.warning(
                f"Failed to send scheduled email {se.pk} to "
                f"{mask_email_for_logging(se.to_email)} "
                f"(attempt {se.attempts}): {e}"
            )
            return False

        se.sent = True
        se.sent_at = now
        se.attempts = se.attempts + 1
        se.save(update_fields=["sent", "sent_at", "attempts"])
        return True

    async def _asend_one(self, candidate: Any) -> bool:
        return await self.adosend(scheduled_email=candidate)

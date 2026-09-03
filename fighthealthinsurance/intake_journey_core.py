"""Core logic for the intake journey's bookkeeping activities.

Same conventions as the other journey cores: plain functions, opaque
identifiers in, all real data loaded from Django at execution time.
"""

from django.conf import settings
from django.core.mail import send_mail

from loguru import logger

from fighthealthinsurance.appeal_journey_core import aload_denial

NUDGE_SUBJECT = "Your appeal on Fight Health Insurance is waiting"
NUDGE_BODY = (
    "You started putting together an appeal on Fight Health Insurance and "
    "didn't get to finish. Your progress is saved -- pick up where you left "
    "off whenever you're ready:\n\n{url}\n\nIf you'd rather not continue, "
    "you can ignore this email; we won't send another reminder."
)


async def send_abandonment_nudge(hashed_email: str, denial_uuid: str) -> bool:
    """Send the single abandonment nudge; returns whether one was sent.

    Consent gate is the RETAINED RAW EMAIL itself: it exists only when the
    user chose store_raw_email, and the clear_expired_emails sweep enforces
    its retention -- so an address that has been cleared (or was never
    stored) simply cannot be nudged. No content from the case is included.
    """
    denial = await aload_denial(hashed_email, denial_uuid)
    if denial is None or not (denial.raw_email or "").strip():
        logger.info(f"Intake nudge skipped for denial {denial_uuid}: no retained email")
        return False
    base = getattr(
        settings, "FHI_PUBLIC_BASE_URL", "https://www.fighthealthinsurance.com"
    )
    # Plain homepage link: no resume handler exists yet, so a ?resume=
    # parameter would be an inert promise -- and a denial uuid does not
    # belong in a URL (mail clients log and preview them). A real resume
    # link needs a signed, expiring token and its own route (external
    # review); that lands with the resume feature itself.
    await _asend_mail(
        NUDGE_SUBJECT,
        NUDGE_BODY.format(url=base),
        denial.raw_email,
    )
    logger.info(f"Intake nudge sent for denial {denial_uuid}")
    return True


async def _asend_mail(subject: str, body: str, to: str) -> None:
    # send_mail is sync network I/O with no ORM: plain asgiref bridge.
    from asgiref.sync import sync_to_async

    await sync_to_async(send_mail, thread_sensitive=False)(
        subject,
        body,
        getattr(settings, "DEFAULT_FROM_EMAIL", None),
        [to],
        fail_silently=False,
    )


async def close_incomplete_journey(hashed_email: str, denial_uuid: str) -> bool:
    """30 days without completion: the incomplete-form hygiene hook.

    v1 records the closure only; whether closed journeys' rows are
    deleted/anonymized is a separate product decision, so nothing
    destructive happens here.
    """
    logger.info(f"Intake journey closed without completion for denial {denial_uuid}")
    return True

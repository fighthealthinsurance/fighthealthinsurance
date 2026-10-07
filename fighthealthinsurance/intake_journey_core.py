"""Core logic for the intake journey's bookkeeping activities.

Same conventions as the other journey cores: plain functions, opaque
identifiers in, all real data loaded from Django at execution time.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse

from loguru import logger

from fighthealthinsurance import intake_resume
from fighthealthinsurance.appeal_journey_core import aload_denial
from fighthealthinsurance.utils import build_fallback_email

NUDGE_SUBJECT = "Your appeal on Fight Health Insurance is waiting"
# emails/intake_nudge.txt and .html hold the words. The link reopens the case
# at the step it reached, after the person types the email address they used
# (intake_resume has the design).
NUDGE_TEMPLATE = "intake_nudge"


async def send_abandonment_nudge(hashed_email: str, denial_uuid: str) -> bool:
    """Send the single abandonment nudge; returns whether one was sent.

    Consent gate is the RETAINED RAW EMAIL itself: it exists only when the
    user chose store_raw_email, and the clear_expired_emails sweep enforces
    its retention -- so an address that has been cleared (or was never
    stored) simply cannot be nudged. No content from the case is included:
    the email carries one resume link, minted only for the claimant, which
    opens nothing without the email address the case was started with
    (intake_resume). While the intake journey is off nothing is claimed,
    minted or sent, and nothing is sent for a case a professional created,
    holds or was added to.
    """
    from fighthealthinsurance import intake_outbox

    if not intake_resume.enabled():
        logger.info(f"Intake nudge skipped for denial {denial_uuid}: journey is off")
        return False
    denial = await aload_denial(hashed_email, denial_uuid)
    if denial is None or not (denial.raw_email or "").strip():
        logger.info(f"Intake nudge skipped for denial {denial_uuid}: no retained email")
        return False
    # The email says "you started" and links to the patient form; a case a
    # professional created, holds or was added to is finished through their
    # pages instead.
    if not await intake_resume.astarted_by_the_person(denial):
        logger.info(
            f"Intake nudge skipped for denial {denial_uuid}: a professional's case"
        )
        return False
    # Single-shot claim: inserting the nudge_claimed event is the claim
    # (unique per denial), so a retried or duplicated activity can never
    # send twice. The claim is NEVER released -- not even after an
    # ambiguous SMTP failure, because "the provider may have accepted it"
    # is exactly the case a release would turn into a second email.
    claim = await intake_outbox.aclaim_nudge(denial)
    if claim is None:
        logger.info(f"Intake nudge skipped for denial {denial_uuid}: already claimed")
        return False
    # Authoritative completion is the outbox record, not workflow state: a
    # form_completed event that is recorded but whose signal is still in
    # flight must not produce a "you didn't finish" email to someone who
    # did. Rechecked immediately before the SMTP call. Honest
    # linearization limit: a completion landing DURING SMTP acceptance
    # cannot be prevented without an idempotent or cancellable provider
    # operation; the claim guarantees at-most-once, and this recheck
    # narrows the window to the send itself (external review).
    if await intake_outbox.ahas_event(denial, intake_outbox.FORM_COMPLETED):
        await intake_outbox.arecord_nudge_outcome(
            claim, intake_outbox.OUTCOME_SKIPPED_COMPLETED
        )
        logger.info(f"Intake nudge skipped for denial {denial_uuid}: form completed")
        return False
    base = getattr(
        settings, "FHI_PUBLIC_BASE_URL", "https://www.fighthealthinsurance.com"
    )
    # Minted after the claim and the completion recheck, so only the one
    # claimant ever mints, and never for a finished form. The token carries
    # nothing about the person or the case; only its digest is stored.
    token = await intake_resume.amint_link(denial)
    url = base.rstrip("/") + reverse("intake_resume_link", args=[token])
    # Sent to the person alone, with no staff copy: the copy would carry the
    # link next to the address that opens it. Built apart from the send, so a
    # template error is recorded as what it is: nothing was sent. The claim
    # stays, as the nudge is single-shot and its activity runs once; a broken
    # template is a deploy bug the render tests catch.
    try:
        message = build_fallback_email(
            NUDGE_SUBJECT,
            NUDGE_TEMPLATE,
            {"url": url, "days": intake_resume.link_days()},
            denial.raw_email,
        )
    except Exception:
        await intake_outbox.arecord_nudge_outcome(
            claim, intake_outbox.OUTCOME_NOT_BUILT
        )
        raise
    try:
        await _asend_message(message)
    except Exception:
        # Ambiguous: the provider may or may not have accepted. Record it,
        # keep the claim, re-raise so the activity reports the failure
        # (its retry policy is one attempt -- no second email either way).
        await intake_outbox.arecord_nudge_outcome(
            claim, intake_outbox.OUTCOME_SMTP_FAILED
        )
        raise
    await intake_outbox.arecord_nudge_outcome(
        claim, intake_outbox.OUTCOME_SENT, sent=True
    )
    logger.info(f"Intake nudge sent for denial {denial_uuid}")
    return True


async def _asend_message(message: EmailMultiAlternatives) -> None:
    # Sending is sync network I/O with no ORM: plain asgiref bridge.
    from asgiref.sync import sync_to_async

    await sync_to_async(message.send, thread_sensitive=False)()


async def close_incomplete_journey(hashed_email: str, denial_uuid: str) -> bool:
    """CLOSE_AFTER (3 days) without completion: the incomplete-form hygiene hook.

    What a closed journey keeps is what the site already says it keeps for a
    case that never reached an appeal, and nothing the journey added:

    - Deleted here: the case's resume point (the step it reached and any
      emailed link). It exists only to bring the person back from the
      reminder, and that is over.
    - Kept, as for every unfinished case: the denial text (the upload page
      says we keep it to improve our AI); the email address, only for
      people who asked us to keep it, until the follow-ups end plus 30 days,
      when clear_expired_emails clears it (the opt-in box says so); the
      follow-up schedule they asked for; and the journey's event rows, which
      hold timestamps only and keep "we won't send another reminder" true.
      All of it goes when the person deletes their data.
    - Cleared only when INTAKE_CLOSED_CASE_CLEARS_HEALTH_HISTORY is on: the
      health history (the page says it is saved "for this appeal") and the
      caches made from it, never for a case whose form was completed, since
      a completion can be recorded while its signal is still in flight
      (``_aclear_health_history``).
    """
    denial = await aload_denial(hashed_email, denial_uuid)
    if denial is None:
        logger.info(f"Intake journey closed for denial {denial_uuid}: no case left")
        return True
    await intake_resume.aforget(denial)
    if getattr(settings, "INTAKE_CLOSED_CASE_CLEARS_HEALTH_HISTORY", False):
        await _aclear_health_history(denial)
    logger.info(f"Intake journey closed without completion for denial {denial_uuid}")
    return True


async def _aclear_health_history(denial) -> None:
    """Clear the health history and the caches made from it, in one UPDATE.

    Scoped to those columns, so nothing else on the row is written back. The
    UPDATE itself requires that no form completion is recorded for the case,
    so a completion recorded at any point before it runs keeps the history.
    """
    from django.db.models import Exists, OuterRef

    from fighthealthinsurance.denial_history_consent import (
        DERIVED_FROM_HEALTH_HISTORY,
    )
    from fighthealthinsurance.models import Denial, IntakeJourneyEvent

    completed = IntakeJourneyEvent.objects.filter(
        denial_id=OuterRef("pk"), event_type=IntakeJourneyEvent.FORM_COMPLETED
    )
    cleared = {name: None for name in ("health_history", *DERIVED_FROM_HEALTH_HISTORY)}
    if await (
        Denial.objects.filter(pk=denial.pk)
        .filter(~Exists(completed))
        .aupdate(**cleared)
    ):
        logger.info(f"Cleared the health history of closed denial {denial.uuid}")

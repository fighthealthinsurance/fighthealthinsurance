"""Temporal activities for ``AssistantAppealWorkflow``.

Asyncio activities on the appeal queue, like the appeal journey's: opaque
identifiers in, sanitized errors out, nothing from the case in history.
"""

import asyncio

from channels.db import database_sync_to_async
from django.core.exceptions import FieldError, ValidationError
from django.db import close_old_connections
from django.db.utils import DataError, ProgrammingError
from loguru import logger
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import assistant_drafts
from fighthealthinsurance.appeal_journey_core import aload_denial
from fighthealthinsurance.ml import spend

_aclose_old_connections = database_sync_to_async(close_old_connections)
_NON_RETRYABLE_ERRORS = (ValidationError, FieldError, ProgrammingError, DataError)

# Each under its activity's start_to_close, so the step ends on its own
# budget with what it has.
READ_TIMEOUT_S = 90
QUESTIONS_TIMEOUT_S = 150


def _non_retryable(e: Exception, denial_uuid: str) -> ApplicationError:
    logger.opt(exception=True).error(
        f"Non-retryable {type(e).__name__} in assistant draft for denial {denial_uuid}"
    )
    return ApplicationError(
        f"{type(e).__name__} for denial {denial_uuid}", non_retryable=True
    )


async def _draft_for(hashed_email: str, denial_uuid: str):
    denial = await aload_denial(hashed_email, denial_uuid)
    if denial is None:
        return None, None
    draft = await database_sync_to_async(assistant_drafts.draft_for_denial)(denial)
    return denial, draft


async def _set_status(draft, status: str) -> None:
    await database_sync_to_async(assistant_drafts.set_status)(draft, status)


@activity.defn
async def read_letter(hashed_email: str, denial_uuid: str) -> bool:
    """Read the letter (extract_entity), then fill what it left empty from
    what the assistant sent. False when there is no such case."""
    await _aclose_old_connections()
    try:
        denial, draft = await _draft_for(hashed_email, denial_uuid)
        if denial is None or draft is None:
            return False
        await _set_status(draft, assistant_drafts.READING)
        from fighthealthinsurance.common_view_logic import DenialCreatorHelper

        try:
            with spend.for_channel(spend.channel_of(denial)):
                async with asyncio.timeout(READ_TIMEOUT_S):
                    async for _ in DenialCreatorHelper.extract_entity(denial.denial_id):
                        pass
        except TimeoutError:
            logger.warning(f"assistant draft: reading timed out for {denial_uuid}")
        await denial.arefresh_from_db(fields=["procedure", "diagnosis"])
        fields = []
        if not denial.procedure and draft.procedure:
            denial.procedure = draft.procedure
            fields.append("procedure")
        if not denial.diagnosis and draft.condition:
            denial.diagnosis = draft.condition
            fields.append("diagnosis")
        if fields:
            await denial.asave(update_fields=fields)
        return True
    except _NON_RETRYABLE_ERRORS as e:
        raise _non_retryable(e, denial_uuid) from None


@activity.defn
async def ask_questions(hashed_email: str, denial_uuid: str) -> int:
    """Generate our questions for this case and keep the askable ones on
    the draft. Returns how many there are; zero means drafting starts."""
    await _aclose_old_connections()
    try:
        denial, draft = await _draft_for(hashed_email, denial_uuid)
        if denial is None or draft is None:
            return 0
        from fighthealthinsurance.common_view_logic import DenialCreatorHelper

        rows = None
        try:
            with spend.for_channel(spend.channel_of(denial)):
                async with asyncio.timeout(QUESTIONS_TIMEOUT_S):
                    rows = await DenialCreatorHelper.generate_appeal_questions(
                        denial.denial_id
                    )
        except TimeoutError:
            logger.warning(f"assistant draft: questions timed out for {denial_uuid}")
        questions = assistant_drafts.clean_questions(rows or [])

        def store() -> None:
            draft.questions = questions
            draft.save(update_fields=["questions"])
            assistant_drafts.set_status(
                draft,
                assistant_drafts.QUESTIONS if questions else assistant_drafts.DRAFTING,
            )

        await database_sync_to_async(store)()
        return len(questions)
    except _NON_RETRYABLE_ERRORS as e:
        raise _non_retryable(e, denial_uuid) from None


@activity.defn
async def start_drafting(hashed_email: str, denial_uuid: str) -> bool:
    """Record the form as completed, so the intake journey sends no nudge,
    and say drafting has begun."""
    await _aclose_old_connections()
    try:
        denial, draft = await _draft_for(hashed_email, denial_uuid)
        if denial is None or draft is None:
            return False
        from fighthealthinsurance import intake_outbox

        await intake_outbox.arecord_intent(denial, intake_outbox.FORM_COMPLETED)
        await _set_status(draft, assistant_drafts.DRAFTING)
        return True
    except _NON_RETRYABLE_ERRORS as e:
        raise _non_retryable(e, denial_uuid) from None


@activity.defn
async def finish_drafts(hashed_email: str, denial_uuid: str) -> str:
    """on_site when the site's own page took the generation, else ready or
    stopped from what the generation actually stored."""
    await _aclose_old_connections()
    try:
        denial, draft = await _draft_for(hashed_email, denial_uuid)
        if denial is None or draft is None:
            return assistant_drafts.STOPPED

        def outcome() -> str:
            if assistant_drafts.site_took_generation(denial):
                return assistant_drafts.ON_SITE
            return assistant_drafts.letters_status(denial, True)

        status = str(await database_sync_to_async(outcome)())
        await _set_status(draft, status)
        return status
    except _NON_RETRYABLE_ERRORS as e:
        raise _non_retryable(e, denial_uuid) from None


@activity.defn
async def mark_draft_status(hashed_email: str, denial_uuid: str, status: str) -> bool:
    await _aclose_old_connections()
    if status not in assistant_drafts.STATUSES:
        raise ApplicationError("unknown draft status", non_retryable=True)
    try:
        denial, draft = await _draft_for(hashed_email, denial_uuid)
        if denial is None or draft is None:
            return False
        await _set_status(draft, status)
        return True
    except _NON_RETRYABLE_ERRORS as e:
        raise _non_retryable(e, denial_uuid) from None

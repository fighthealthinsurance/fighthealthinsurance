"""Core logic for the durable appeal-generation journey.

Mirrors :mod:`fighthealthinsurance.fax_send_core`: plain synchronous functions
that the Temporal activities in
:mod:`fighthealthinsurance.activities.appeal_journey` wrap thinly. Everything
re-loads the denial from opaque identifiers ``(hashed_email, denial_uuid)`` so
no PHI crosses the workflow boundary.

This is the queued/async path: generate appeal drafts in the background and
persist them as ``ProposedAppeal`` rows for the user to find. The interactive
streaming path (``AppealsBackendHelper.generate_appeals`` over websockets)
stays exactly as it is; this journey reuses it as a library so the prompt
assembly, context gathering and substitutions have one implementation.
"""

import asyncio
import json
from typing import AsyncGenerator, Optional, cast

from asgiref.sync import async_to_sync

from loguru import logger

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_NO_DENIAL_TEXT = "no_denial_text"
STATUS_ALREADY_HAS_APPEALS = "already_has_appeals"

# How many drafts one journey run aims to persist, and how long the generation
# step may spend before returning with whatever it has. The activity's
# start_to_close is set above this so a full budget is never cut short.
TARGET_APPEALS = 3
GENERATION_BUDGET_SECONDS = 240


def load_denial(hashed_email: str, denial_uuid: str):
    from django.core.exceptions import ValidationError

    from fighthealthinsurance.models import Denial

    try:
        return Denial.objects.filter(hashed_email=hashed_email, uuid=denial_uuid).get()
    except Denial.DoesNotExist:
        logger.warning(f"Appeal journey: no denial for uuid={denial_uuid}")
        return None
    except (ValidationError, ValueError):
        # A malformed uuid fails field validation BEFORE DoesNotExist applies;
        # left unhandled it escapes into the precheck's unlimited retry and
        # poisons the workflow (PR #963 review). Same terminal answer.
        logger.warning(f"Appeal journey: invalid denial uuid {denial_uuid!r}")
        return None


def precheck_appeal_journey(denial) -> str:
    """Validate the denial can produce appeals; cheap and side-effect free."""
    from fighthealthinsurance.models import ProposedAppeal

    if not (denial.denial_text or "").strip():
        return STATUS_NO_DENIAL_TEXT
    # speculative=True rows are the background precompute held in reserve;
    # the interactive flow doesn't count them as delivered appeals and
    # neither does the journey (PR #963 review).
    existing = ProposedAppeal.objects.filter(
        for_denial=denial, speculative=False
    ).count()
    if existing >= TARGET_APPEALS:
        # A retry after a crash, or a duplicate dispatch: the drafts are
        # already there, so the journey is idempotently done.
        return STATUS_ALREADY_HAS_APPEALS
    return STATUS_OK


class JourneyIncomplete(Exception):
    """Raised when a run ends with fewer durable drafts than the target.

    Surfacing this (instead of returning a partial count) is what makes the
    activity's retry policy meaningful: a swallowed budget timeout, a failed
    save inside the generator, or an empty stream must look like a retryable
    failure, not success (PR #963 review).
    """


def generate_and_store_appeals(denial) -> int:
    """Drive the shared generator until enough drafts exist; returns how many
    new drafts got persisted.

    ``AppealsBackendHelper.generate_appeals`` persists every generated draft
    itself (``save_appeal``) before yielding its frame, so this function only
    CONSUMES the stream within a bounded budget -- it must never create rows
    from the streamed output, or every draft would be stored twice.

    Progress is measured in durable row IDENTITIES, never yielded text: the
    generator re-serves existing drafts transformed by ``sub_in_appeals`` (so
    their text differs from what is stored) and can yield content whose save
    FAILED -- both fooled a text-based count into stopping early with nothing
    persisted (PR #963 review). The postcondition is enforced against the
    database; falling short raises :class:`JourneyIncomplete` so the activity
    retries instead of reporting a hollow success.
    """
    from fighthealthinsurance.common_view_logic import AppealsBackendHelper
    from fighthealthinsurance.models import ProposedAppeal

    parameters = {
        "denial_id": denial.denial_id,
        "email": None,
        "hashed_email": denial.hashed_email,
        "semi_sekret": denial.semi_sekret,
    }

    async def _stored_ids() -> set:
        # Non-speculative only: reserve precompute rows don't count as
        # delivered drafts anywhere else either. Native async ORM (no
        # bridge) per repo convention.
        return {
            pk
            async for pk in ProposedAppeal.objects.filter(
                for_denial=denial, speculative=False
            ).values_list("id", flat=True)
        }

    async def _run() -> tuple:
        # Every DB read lives inside this one async context: the generator's
        # internal connection cleanup can close sync-thread connections, so a
        # sync-side query after consumption is not safe to rely on.
        baseline = await _stored_ids()
        if len(baseline) >= TARGET_APPEALS:
            return baseline, baseline

        frames = 0
        # generate_appeals is an async generator (declared AsyncIterator), so
        # aclose() exists at runtime; the cast lets mypy see it.
        agen = cast(
            AsyncGenerator[str, None], AppealsBackendHelper.generate_appeals(parameters)
        )
        try:
            async with asyncio.timeout(GENERATION_BUDGET_SECONDS):
                async for chunk in agen:
                    if _appeal_text_from_chunk(chunk) is None:
                        continue
                    # Early stop on DURABLE progress: appeal frames are few,
                    # so a count query per frame is cheap, and only rows that
                    # actually persisted can end the consumption early.
                    frames += 1
                    if len(await _stored_ids()) >= TARGET_APPEALS:
                        break
        except TimeoutError:
            logger.info(
                f"Appeal journey: generation budget reached after {frames} "
                f"appeal frame(s) for denial {denial.uuid}"
            )
        finally:
            await agen.aclose()
        return baseline, await _stored_ids()

    # async_to_sync (not asyncio.run): asgiref keeps the sync-thread context,
    # so the generator's thread_sensitive ORM bridges execute on THIS thread
    # and its Django connection, matching how the interactive stack drives it.
    baseline, after = async_to_sync(_run)()
    stored = len(after - baseline)
    logger.info(
        f"Appeal journey: {stored} new draft(s) persisted for denial "
        f"uuid={denial.uuid}"
    )
    if len(after) < TARGET_APPEALS:
        raise JourneyIncomplete(
            f"{len(after)} of {TARGET_APPEALS} drafts durable for denial "
            f"{denial.uuid} (stored {stored} this attempt)"
        )
    return stored


def _appeal_text_from_chunk(chunk: str) -> Optional[str]:
    """The generator yields JSON strings; pull the appeal text out defensively."""
    try:
        data = json.loads(chunk)
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        text = data.get("content") or data.get("appeal") or data.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None

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
    from fighthealthinsurance.models import Denial

    try:
        return Denial.objects.filter(hashed_email=hashed_email, uuid=denial_uuid).get()
    except Denial.DoesNotExist:
        logger.warning(f"Appeal journey: no denial for uuid={denial_uuid}")
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


def generate_and_store_appeals(denial) -> int:
    """Drive the shared generator until enough drafts exist; returns how many
    new drafts got persisted.

    ``AppealsBackendHelper.generate_appeals`` persists every generated draft
    itself (``save_appeal``) before yielding its frame, so this function only
    CONSUMES the stream within a bounded budget and reports the persisted
    delta -- it must never create rows from the streamed output, or every
    draft would be stored twice (PR #963 review). The yielded texts are used
    solely to stop consuming early once enough new drafts have appeared.
    """
    from fighthealthinsurance.common_view_logic import AppealsBackendHelper
    from fighthealthinsurance.models import ProposedAppeal

    def _stored_count() -> int:
        # Non-speculative only: reserve precompute rows don't count as
        # delivered drafts anywhere else either.
        return ProposedAppeal.objects.filter(
            for_denial=denial, speculative=False
        ).count()

    existing_texts = set(
        ProposedAppeal.objects.filter(for_denial=denial, speculative=False).values_list(
            "appeal_text", flat=True
        )
    )
    before = _stored_count()
    needed = TARGET_APPEALS - before
    if needed <= 0:
        return 0

    parameters = {
        "denial_id": denial.denial_id,
        "email": None,
        "hashed_email": denial.hashed_email,
        "semi_sekret": denial.semi_sekret,
    }

    async def _consume() -> None:
        new_texts: set = set()
        # generate_appeals is an async generator (declared AsyncIterator), so
        # aclose() exists at runtime; the cast lets mypy see it.
        agen = cast(
            AsyncGenerator[str, None], AppealsBackendHelper.generate_appeals(parameters)
        )
        try:
            async with asyncio.timeout(GENERATION_BUDGET_SECONDS):
                async for chunk in agen:
                    text = _appeal_text_from_chunk(chunk)
                    # The generator re-serves existing drafts first; only
                    # genuinely new texts count toward the early stop.
                    if text and text not in existing_texts:
                        new_texts.add(text)
                    if len(new_texts) >= needed:
                        break
        except TimeoutError:
            logger.info(
                f"Appeal journey: generation budget reached with "
                f"{len(new_texts)} new draft(s) seen for denial {denial.uuid}"
            )
        finally:
            await agen.aclose()

    asyncio.run(_consume())
    stored = max(0, _stored_count() - before)
    logger.info(
        f"Appeal journey: {stored} new draft(s) persisted for denial "
        f"uuid={denial.uuid}"
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

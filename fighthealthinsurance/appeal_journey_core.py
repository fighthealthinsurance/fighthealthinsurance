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
    existing = ProposedAppeal.objects.filter(for_denial=denial).count()
    if existing >= TARGET_APPEALS:
        # A retry after a crash, or a duplicate dispatch: the drafts are
        # already there, so the journey is idempotently done.
        return STATUS_ALREADY_HAS_APPEALS
    return STATUS_OK


def generate_and_store_appeals(denial) -> int:
    """Generate appeal drafts and persist them; returns how many were stored.

    Consumes the same async generator the interactive flow streams from, with
    a bounded budget, and dedupes against drafts already stored for this
    denial so an activity retry cannot double-store.
    """
    from fighthealthinsurance.common_view_logic import AppealsBackendHelper
    from fighthealthinsurance.models import ProposedAppeal

    existing_texts = set(
        ProposedAppeal.objects.filter(for_denial=denial).values_list(
            "appeal_text", flat=True
        )
    )
    needed = TARGET_APPEALS - len(existing_texts)
    if needed <= 0:
        return 0

    parameters = {
        "denial_id": denial.denial_id,
        "email": None,
        "hashed_email": denial.hashed_email,
        "semi_sekret": denial.semi_sekret,
    }

    async def _collect() -> list:
        collected: list = []
        # generate_appeals is an async generator (declared AsyncIterator), so
        # aclose() exists at runtime; the cast lets mypy see it.
        agen = cast(
            AsyncGenerator[str, None], AppealsBackendHelper.generate_appeals(parameters)
        )
        try:
            async with asyncio.timeout(GENERATION_BUDGET_SECONDS):
                async for chunk in agen:
                    text = _appeal_text_from_chunk(chunk)
                    if text and text not in existing_texts:
                        collected.append(text)
                        existing_texts.add(text)
                    if len(collected) >= needed:
                        break
        except TimeoutError:
            logger.info(
                f"Appeal journey: generation budget reached with "
                f"{len(collected)} new draft(s) for denial {denial.uuid}"
            )
        finally:
            await agen.aclose()
        return collected

    stored = 0
    for text in asyncio.run(_collect()):
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=text)
        stored += 1
    logger.info(
        f"Appeal journey: stored {stored} draft(s) for denial uuid={denial.uuid}"
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

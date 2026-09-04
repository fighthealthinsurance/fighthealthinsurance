"""Core logic for the durable appeal-generation journey.

Mirrors :mod:`fighthealthinsurance.fax_send_core`: plain functions that the
Temporal activities in :mod:`fighthealthinsurance.activities.appeal_journey`
wrap thinly. Everything re-loads the denial from opaque identifiers
``(hashed_email, denial_uuid)`` so no PHI crosses the workflow boundary.

Async-first: the activities are asyncio activities (cooperative cancellation,
no unowned worker thread), so the primary API here is the ``a``-prefixed
coroutines using Django's native async ORM. The sync wrappers exist for
tests and non-async callers.

This is the queued/async path: generate appeal drafts in the background and
persist them as ``ProposedAppeal`` rows for the user to find. The interactive
streaming path (``AppealsBackendHelper.generate_appeals`` over websockets)
stays exactly as it is; this journey reuses it through the internal entry
point ``generate_appeals_for_denial`` so prompt assembly, context gathering
and substitutions have one implementation.
"""

import asyncio
import json
from typing import AsyncGenerator, Optional, cast

from asgiref.sync import async_to_sync

from loguru import logger

from fighthealthinsurance import generation_lease
from fighthealthinsurance.utils import is_real_appeal

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_NO_DENIAL_TEXT = "no_denial_text"
STATUS_ALREADY_HAS_APPEALS = "already_has_appeals"

# How many drafts one journey run aims to persist, and how long the generation
# step may spend before returning with whatever it has. The activity's
# start_to_close is set above this so a full budget is never cut short.
TARGET_APPEALS = 3
GENERATION_BUDGET_SECONDS = 240


class JourneyIncomplete(Exception):
    """Raised when a run ends with fewer durable drafts than the target.

    Surfacing this (instead of returning a partial count) is what makes the
    activity's retry policy meaningful: a swallowed budget timeout, a failed
    save inside the generator, or an empty stream must look like a retryable
    failure, not success (PR #963 review).
    """


class LeaseHeld(Exception):
    """Raised when another generator currently owns the denial's lease.

    Retryable by design: by the activity's next attempt the other owner has
    either delivered (the baseline check then ends the journey with zero
    work) or died (its lease expired and the journey proceeds). Nobody ever
    waits on the lock (external review; generation_lease.py).
    """


# How often a running journey pushes its lease expiry out; well inside the
# lease TTL so a live attempt never expires mid-run while a dead one frees
# the denial within one TTL.
LEASE_EXTEND_INTERVAL_S = 10.0


async def acheck_generation_postcondition(denial) -> bool:
    """Is the durable outcome the journey exists for already true: a chosen
    appeal, or the target number of real drafts? Used by the intake
    workflow after a child-start collision instead of assuming the other
    generation run succeeded (external review)."""
    return (await aprecheck_appeal_journey(denial)) == STATUS_ALREADY_HAS_APPEALS


def check_generation_postcondition(denial) -> bool:
    return async_to_sync(acheck_generation_postcondition)(denial)


async def aload_denial(hashed_email: str, denial_uuid: str):
    from django.core.exceptions import ValidationError

    from fighthealthinsurance.models import Denial

    try:
        return await Denial.objects.filter(
            hashed_email=hashed_email, uuid=denial_uuid
        ).aget()
    except Denial.DoesNotExist:
        logger.warning(f"Appeal journey: no denial for uuid={denial_uuid}")
        return None
    except (ValidationError, ValueError):
        # A malformed uuid fails field validation BEFORE DoesNotExist applies;
        # left unhandled it escapes into the precheck's unlimited retry and
        # poisons the workflow (PR #963 review). Same terminal answer.
        logger.warning(f"Appeal journey: invalid denial uuid {denial_uuid!r}")
        return None


def load_denial(hashed_email: str, denial_uuid: str):
    return async_to_sync(aload_denial)(hashed_email, denial_uuid)


async def aprecheck_appeal_journey(denial) -> str:
    """Validate the denial can produce appeals; cheap and side-effect free."""
    from fighthealthinsurance.models import ProposedAppeal

    if not (denial.denial_text or "").strip():
        return STATUS_NO_DENIAL_TEXT
    # A chosen row means the user already picked their appeal: the journey
    # is complete regardless of draft counts (mark_proposal_chosen CREATES
    # an extra chosen=True copy, so counting it as a draft both inflates
    # and short-circuits the target -- PR review).
    if await ProposedAppeal.objects.filter(for_denial=denial, chosen=True).aexists():
        return STATUS_ALREADY_HAS_APPEALS
    # speculative=True rows are the background precompute held in reserve;
    # the interactive flow doesn't count them as delivered appeals and
    # neither does the journey (PR #963 review). Counted as DISTINCT
    # fingerprints, not rows: the pre-constraint era double-stored drafts,
    # and three copies of one letter are one deliverable draft, not three.
    # NULL fingerprints are exactly the rows the backfill migration left
    # as known legacy duplicates, so they never count (external review).
    existing_fingerprints = set()
    async for text, fp in ProposedAppeal.objects.filter(
        for_denial=denial,
        speculative=False,
        chosen=False,
        text_fingerprint__isnull=False,
    ).values_list("appeal_text", "text_fingerprint"):
        if is_real_appeal(text):
            existing_fingerprints.add(fp)
    if len(existing_fingerprints) >= TARGET_APPEALS:
        # A retry after a crash, or a duplicate dispatch: the drafts are
        # already there, so the journey is idempotently done.
        return STATUS_ALREADY_HAS_APPEALS
    return STATUS_OK


def precheck_appeal_journey(denial) -> str:
    return async_to_sync(aprecheck_appeal_journey)(denial)


async def agenerate_and_store_appeals(denial) -> int:
    """Drive the shared generator until enough drafts exist; returns how many
    new drafts got persisted.

    The generator persists every generated draft itself (``save_appeal``)
    before yielding its frame, so this function only CONSUMES the stream
    within a bounded budget -- it must never create rows from the streamed
    output, or every draft would be stored twice.

    Progress is measured in durable DISTINCT content fingerprints, never
    yielded text: the generator re-serves existing drafts transformed by
    ``sub_in_appeals`` (so their text differs from what is stored) and can
    yield content whose save FAILED -- both fooled a text-based count into
    stopping early with nothing persisted (PR #963 review). Fingerprints
    rather than row ids so duplicate rows count once (external review). The postcondition is enforced against the
    database; falling short raises :class:`JourneyIncomplete` so the activity
    retries instead of reporting a hollow success.

    Cancellation is cooperative: an ``asyncio.CancelledError`` from the
    activity propagates into the ``async for`` and the ``finally`` closes the
    generator, so a canceled or timed-out attempt does not leave the model
    call running unowned.
    """
    from fighthealthinsurance.common_view_logic import AppealsBackendHelper
    from fighthealthinsurance.models import ProposedAppeal

    async def _stored_fingerprints() -> set:
        # Deliverable candidates only: non-speculative (reserves are not
        # delivered drafts), un-chosen (a chosen row is a COPY the pick
        # flow creates, not a candidate), and real letters (legacy empty/
        # runt rows must not satisfy the target). Measured as DISTINCT
        # fingerprints rather than row ids so duplicate rows -- historical
        # double-stores, or a racing writer on a database whose constraint
        # enforcement lapsed -- count as the one draft they are; NULL
        # fingerprints are the backfill's known-legacy-duplicate marker and
        # never count (external review). Native async ORM.
        return {
            fp
            async for fp, text in ProposedAppeal.objects.filter(
                for_denial=denial,
                speculative=False,
                chosen=False,
                text_fingerprint__isnull=False,
            ).values_list("text_fingerprint", "appeal_text")
            if is_real_appeal(text)
        }

    baseline = await _stored_fingerprints()
    if len(baseline) >= TARGET_APPEALS:
        return 0

    # Single-writer boundary: take the denial's generation lease (never
    # stealing -- a live interactive run outranks a background job). Held
    # means another generator owns it right now; back off via the retry
    # policy rather than double-generating (external review).
    lease = await generation_lease.aacquire(
        denial, holder=generation_lease.new_holder("journey")
    )
    if not lease.acquired:
        raise LeaseHeld(
            f"generation lease held for denial {denial.uuid} (epoch {lease.epoch})"
        )

    async def _keep_lease() -> None:
        # Owned by this attempt: cancelled in the finally below, so a dead
        # attempt stops extending and the lease expires within one TTL.
        while True:
            await asyncio.sleep(LEASE_EXTEND_INTERVAL_S)
            if not await generation_lease.aextend(denial, lease.epoch):
                return  # stolen: the per-frame check ends the run

    extender = asyncio.create_task(_keep_lease())
    frames = 0
    stolen = False
    # generate_appeals is an async generator (declared AsyncIterator), so
    # aclose() exists at runtime; the cast lets mypy see it.
    agen = cast(
        AsyncGenerator[str, None],
        # The epoch rides into the generator so save_appeal fences every
        # draft insert on it: the per-frame check below is the early stop,
        # the write boundary is the guarantee (review).
        AppealsBackendHelper.generate_appeals_for_denial(
            denial, lease_epoch=lease.epoch
        ),
    )
    try:
        async with asyncio.timeout(GENERATION_BUDGET_SECONDS):
            async for chunk in agen:
                if _appeal_text_from_chunk(chunk) is None:
                    continue
                # Early stop on DURABLE progress: appeal frames are few, so a
                # count query per frame is cheap, and only rows that actually
                # persisted can end the consumption early.
                frames += 1
                if await generation_lease.acurrent_epoch(denial) != lease.epoch:
                    # Fencing token moved: an interactive run stole the
                    # lease. Stop quietly with what is stored; the user is
                    # being served live and the retry's baseline check will
                    # see their drafts.
                    stolen = True
                    break
                # Distinct fingerprints, not row ids: duplicate rows are one
                # deliverable draft (the fingerprint work now on main).
                if len(await _stored_fingerprints()) >= TARGET_APPEALS:
                    break
    except TimeoutError:
        logger.info(
            f"Appeal journey: generation budget reached after {frames} "
            f"appeal frame(s) for denial {denial.uuid}"
        )
    finally:
        extender.cancel()
        await agen.aclose()
        await generation_lease.arelease(denial, lease.epoch)

    after = await _stored_fingerprints()
    stored = len(after - baseline)
    logger.info(
        f"Appeal journey: {stored} new draft(s) persisted for denial "
        f"uuid={denial.uuid}"
    )
    if stolen:
        logger.info(
            f"Appeal journey: lease stolen mid-run for denial {denial.uuid}; "
            "deferring to the interactive generator"
        )
        return stored
    if len(after) < TARGET_APPEALS:
        raise JourneyIncomplete(
            f"{len(after)} of {TARGET_APPEALS} drafts durable for denial "
            f"{denial.uuid} (stored {stored} this attempt)"
        )
    return stored


def generate_and_store_appeals(denial) -> int:
    # async_to_sync (not asyncio.run): asgiref keeps the sync-thread context,
    # so the generator's thread_sensitive ORM bridges execute on the calling
    # thread and its Django connection, matching the interactive stack.
    return async_to_sync(agenerate_and_store_appeals)(denial)


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

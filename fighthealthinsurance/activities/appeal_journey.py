"""Temporal activities for the queued appeal-generation journey.

Asyncio activities (unlike the fax activities' sync-thread pattern): the
work is natively async, so Temporal owns the whole execution -- cancellation
and timeouts deliver ``asyncio.CancelledError`` into the coroutine, the
generator is closed cooperatively, and no unowned daemon thread can outlive
its attempt and race a retry (PR #963 review). Heartbeats come from an owned
coroutine that dies with the attempt.

Conventions shared with the fax activities: opaque identifiers only, and
sanitized exceptions so no case content enters workflow history.
"""

import asyncio

from loguru import logger

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance.appeal_journey_core import (
    STATUS_NOT_FOUND,
    JourneyIncomplete,
    agenerate_and_store_appeals,
    aload_denial,
    aprecheck_appeal_journey,
)

HEARTBEAT_INTERVAL_S = 10.0


async def _heartbeats():
    """Liveness-only heartbeat loop; cancelled when its attempt ends."""
    while True:
        try:
            activity.heartbeat()
        except RuntimeError:
            # Not inside an activity (direct call in tests): nothing to beat.
            return
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


@activity.defn
async def precheck_appeal_journey(hashed_email: str, denial_uuid: str) -> str:
    denial = await aload_denial(hashed_email, denial_uuid)
    if denial is None:
        return STATUS_NOT_FOUND
    return await aprecheck_appeal_journey(denial)


@activity.defn
async def generate_and_store_appeals(hashed_email: str, denial_uuid: str) -> int:
    """Generate + persist drafts; returns the number stored this attempt."""
    beat = asyncio.create_task(_heartbeats())
    try:
        denial = await aload_denial(hashed_email, denial_uuid)
        if denial is None:
            return 0
        try:
            return int(await agenerate_and_store_appeals(denial))
        except JourneyIncomplete as e:
            # Counts and uuid only -- safe for history, and retryable by
            # design: the durable postcondition was not met.
            raise ApplicationError(str(e)) from None
        except asyncio.CancelledError:
            # Cancellation/timeout: the core's finally already closed the
            # generator; let Temporal see the cancellation itself.
            raise
        except Exception:
            # Keep detail in worker logs; history gets only the opaque uuid.
            logger.opt(exception=True).error(
                f"Appeal generation failed for denial {denial_uuid}"
            )
            raise ApplicationError(
                f"appeal generation failed for denial {denial_uuid}"
            ) from None
    finally:
        beat.cancel()

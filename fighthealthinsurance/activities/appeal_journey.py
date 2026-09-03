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

from channels.db import database_sync_to_async
from django.core.exceptions import FieldError, ValidationError
from django.db import close_old_connections
from django.db.utils import DataError, ProgrammingError

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

# Activity-entry connection hygiene, mirroring the fax activities: a
# long-lived worker's ORM lane can hold a server-closed connection between
# attempts, and the first query of a new attempt would then fail as if it
# were a real error. Django 5.2 has no native async variant, so bridge the
# sync one; database_sync_to_async runs it on the same thread-sensitive
# lane the native async ORM uses, which is exactly the connection to check.
_aclose_old_connections = database_sync_to_async(close_old_connections)

# Failures that no retry can fix: schema drift, bad field references,
# invalid values. Retrying these forever (the precheck's retry policy is
# unbounded by design) would keep a broken workflow open while classifying
# a programming error as transient (external review). OperationalError and
# InterfaceError stay retryable: those ARE the transient class.
_NON_RETRYABLE_ERRORS = (ValidationError, FieldError, ProgrammingError, DataError)


def _non_retryable(e: Exception, denial_uuid: str) -> ApplicationError:
    # type name + opaque uuid only; detail stays in worker logs.
    logger.opt(exception=True).error(
        f"Non-retryable {type(e).__name__} in appeal journey for denial {denial_uuid}"
    )
    return ApplicationError(
        f"{type(e).__name__} for denial {denial_uuid}", non_retryable=True
    )


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
    await _aclose_old_connections()
    try:
        denial = await aload_denial(hashed_email, denial_uuid)
        if denial is None:
            return STATUS_NOT_FOUND
        return await aprecheck_appeal_journey(denial)
    except _NON_RETRYABLE_ERRORS as e:
        raise _non_retryable(e, denial_uuid) from None


@activity.defn
async def generate_and_store_appeals(hashed_email: str, denial_uuid: str) -> int:
    """Generate + persist drafts; returns the number stored this attempt."""
    beat = asyncio.create_task(_heartbeats())
    try:
        await _aclose_old_connections()
        denial = await aload_denial(hashed_email, denial_uuid)
        if denial is None:
            return 0
        try:
            return int(await agenerate_and_store_appeals(denial))
        except _NON_RETRYABLE_ERRORS as e:
            raise _non_retryable(e, denial_uuid) from None
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

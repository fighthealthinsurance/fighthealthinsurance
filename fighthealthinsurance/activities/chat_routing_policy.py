"""Temporal activity that writes one chat routing policy row.

The work itself is ``ml/chat_policy.compute_and_store_chat_policy``, the
same function the ``compute_chat_policy`` command runs: read ChatTurn
metadata over a window, compute the policy, append one ChatRoutingPolicy
row and prune rows older than 30 days. This wrapper only adds the Temporal
conventions shared with the appeal-journey activities: fresh database
connections at entry, schema and validation errors classed as
non-retryable, and failures reported by exception class name only.

Everything that crosses into workflow history is a number: the window in,
the new row's id out. No chat text is read here, and no exception message
is logged or raised, since a message could quote whatever a query touched.
"""

from channels.db import database_sync_to_async
from loguru import logger
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance.activities.appeal_journey import (
    _NON_RETRYABLE_ERRORS,
    _aclose_old_connections,
)
from fighthealthinsurance.ml import chat_policy

# The same bounds the compute_chat_policy command accepts.
MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 30 * 24 * 60


def _store(window_minutes: int) -> int:
    from fighthealthinsurance.models import ChatRoutingPolicy

    row = chat_policy.compute_and_store_chat_policy(
        window_minutes=window_minutes, source=ChatRoutingPolicy.Source.TEMPORAL
    )
    return int(row.pk)


_astore = database_sync_to_async(_store)


@activity.defn
async def compute_and_store_chat_policy(window_minutes: int) -> int:
    """Compute a chat routing policy from the last ``window_minutes`` of
    chat turns, store it as a new ChatRoutingPolicy row (source "temporal")
    and return the row's id."""
    if (
        isinstance(window_minutes, bool)
        or not isinstance(window_minutes, int)
        or not MIN_WINDOW_MINUTES <= window_minutes <= MAX_WINDOW_MINUTES
    ):
        # A bad input stays bad on every attempt.
        raise ApplicationError(
            "window_minutes out of range", non_retryable=True
        ) from None
    await _aclose_old_connections()
    try:
        return int(await _astore(window_minutes))
    except _NON_RETRYABLE_ERRORS as e:
        name = type(e).__name__
        logger.error(f"Chat routing policy not stored: {name} (not retried)")
        raise ApplicationError(
            f"{name} storing the chat routing policy", non_retryable=True
        ) from None
    except Exception as e:
        # Database hiccups and the like: retryable. The schedule's next run
        # is the real retry if the attempts run out.
        name = type(e).__name__
        logger.warning(f"Chat routing policy not stored: {name}")
        raise ApplicationError(f"{name} storing the chat routing policy") from None

"""Temporal activities for ``PlanDocumentsSweepWorkflow``.

Asyncio activities on the plan documents queue, with the same conventions
as the reading's (activities/shared_plan_documents.py): numbers and ids in,
ids, uuids and booleans out, errors reported by class name only, schema and
bad-input errors never retried.

The reminder's address never leaves send_plan_documents_reminder: it is
read from the row the activity claims, by deleting it, and used for the one
email. The workflow runs that activity once, never retried, so a reminder
goes at most once, as it does from the Ray email loop.
"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance import plan_documents_reminder as reminders
from fighthealthinsurance.activities.appeal_journey import (
    _NON_RETRYABLE_ERRORS,
    _aclose_old_connections,
)
from fighthealthinsurance.activities.shared_plan_documents import _failure

# The reminder lookup returns the 100 oldest due at most.
MAX_REMINDERS = 100
MAX_BATCHES = 500
MAX_RECEIVED_MINUTES = 7 * 24 * 60


def _checked_int(value: int, lowest: int, highest: int, what: str) -> int:
    """``value`` when it is a whole number in range; never retried if not."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApplicationError(f"{what} is not a number", non_retryable=True)
    if not lowest <= value <= highest:
        raise ApplicationError(f"{what} out of range", non_retryable=True)
    return value


@activity.defn
async def find_due_plan_documents_reminders(limit: int) -> list[int]:
    """The ids of up to ``limit`` reminders that are due, oldest first."""
    limit = _checked_int(limit, 1, MAX_REMINDERS, "limit")
    await _aclose_old_connections()
    try:
        return await reminders.adue_reminder_ids(limit)
    except _NON_RETRYABLE_ERRORS as e:
        raise _failure(e, "due reminders", retryable=False) from None
    except Exception as e:
        raise _failure(e, "due reminders") from None


@activity.defn
async def send_plan_documents_reminder(reminder_id: int) -> bool:
    """Claim one reminder and send its email; False when there was nothing
    to send (not due, claimed already, removed) or the send failed."""
    reminder_id = _checked_int(reminder_id, 1, 2**63 - 1, "reminder id")
    await _aclose_old_connections()
    try:
        return await reminders.asend_reminder(reminder_id)
    except Exception as e:
        # Never retried: the row may already be claimed and the mail sent.
        raise _failure(e, f"reminder {reminder_id}", retryable=False) from None


@activity.defn
async def find_waiting_share_batches(received_minutes: int, limit: int) -> list[str]:
    """Up to ``limit`` share batches, oldest first, with a row shared more
    than ``received_minutes`` ago that is still waiting to be read."""
    received_minutes = _checked_int(
        received_minutes, 1, MAX_RECEIVED_MINUTES, "received_minutes"
    )
    limit = _checked_int(limit, 1, MAX_BATCHES, "limit")
    await _aclose_old_connections()
    try:
        return await processing.awaiting_batches(received_minutes, limit)
    except _NON_RETRYABLE_ERRORS as e:
        raise _failure(e, "waiting shares", retryable=False) from None
    except Exception as e:
        raise _failure(e, "waiting shares") from None

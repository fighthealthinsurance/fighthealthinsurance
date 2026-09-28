"""Background shadow scoring of a delivered chat turn (ml/chat_shadow.py).

``start`` is the one entry point the chat uses, after the reply frame has
gone out and the turn's ChatTurn row has been written. It never awaits and
never raises: it either starts one background task or does nothing. The task
collects the identifiers held for the chat and the records linked to it
(chat/redaction.py), asks TypeSafe through chat_shadow.score_turn, notes the
outcome on the ExternalServiceHealth row the status page reads, and writes
the scores, the scorer string and the outcome onto the turn's row.

Nothing starts unless the person allowed outside models for the chat, the
TYPESAFE_CHAT_SHADOW_ENABLED flag is on and the TypeSafe key is set. The
task holds the texts in memory only, for as long as it runs; nothing here
stores or logs them. Errors are logged by class name only.

Bounded, and kept off the chat's own work:

* At most MAX_IN_FLIGHT tasks per process, and at most DB_THREAD_LIMIT
  (two per task) of their database threads running, those a timed-out
  step left behind included (a turn over either limit is not scored). The thread limit is
  checked again as each step starts its thread, so tasks admitted together
  cannot overshoot it, and the score write's place is reserved before
  anything is sent, so a task that sends always has somewhere to store the
  answer.
* Each database step (the identifier lookup, then the health note with
  the score write) runs through chat/isolated_db.py: on a thread with its
  own connection, never on the chat's thread-sensitive executor, waiting
  at most DB_STEP_SECONDS, with its statements ended at the same bound on
  PostgreSQL. The texts never reach those threads.
* Each request waits at most TYPESAFE_TIMEOUT_SECONDS, and the request step
  as a whole chat_shadow.GRACE_SECONDS more.
* The whole job stops at the sum of those bounds plus JOB_SPARE_SECONDS,
  whatever it is waiting on.
"""

import asyncio
import uuid
from typing import Any, Dict, Optional, Set

from django.conf import settings
from loguru import logger

from fighthealthinsurance.chat import isolated_db
from fighthealthinsurance.chat.redaction import chat_redactions
from fighthealthinsurance.ml import chat_shadow

# Per process. Chat turns are slow and few next to this, so a full slot
# table means TypeSafe or the database is slow; dropping a shadow score
# then costs nothing.
MAX_IN_FLIGHT = 8
# Database-thread places: a task holds its reserved score-write place and,
# during the identifier lookup, one more, so two per task lets all
# MAX_IN_FLIGHT tasks run at once.
DB_THREAD_LIMIT = 2 * MAX_IN_FLIGHT

# The bound on each database step, both the wait and (on PostgreSQL) each
# statement. These are small primary-key reads and one update.
DB_STEP_SECONDS = 5.0
# Added to the steps' bounds for the bound on the whole job.
JOB_SPARE_SECONDS = 1.0
# The name of this module's database threads (isolated_db.running).
DB_THREAD_NAME = "fhi-chat-shadow-db"

# Strong references: a task the event loop only holds weakly could be
# collected mid-request.
_in_flight: Set["asyncio.Task[None]"] = set()


def _shadow_fields(result: chat_shadow.ShadowResult) -> Dict[str, Any]:
    """The ChatTurn columns for one result: floats, the scorer string and
    the outcome. Nothing else can get onto the row from here."""
    winner = result.winner if result.outcome == chat_shadow.SCORED else None
    second = result.second if result.outcome == chat_shadow.SCORED else None
    return {
        "shadow_outcome": result.outcome,
        "shadow_scorer": result.scorer if winner is not None else "",
        "shadow_winner_answers": winner.answers if winner else None,
        "shadow_winner_verdict": winner.verdict if winner else None,
        "shadow_winner_asks_again": winner.asks_again if winner else None,
        "shadow_winner_promises": winner.promises if winner else None,
        "shadow_second_answers": second.answers if second else None,
        "shadow_second_verdict": second.verdict if second else None,
        "shadow_second_asks_again": second.asks_again if second else None,
        "shadow_second_promises": second.promises if second else None,
    }


def _store_sync(chat_id: Any, turn_id: uuid.UUID, fields: Dict[str, Any]) -> int:
    from fighthealthinsurance.models import ChatTurn

    # Only a row of this chat that has no shadow outcome yet: a turn is
    # scored once.
    return ChatTurn.objects.filter(
        pk=turn_id, chat_id=chat_id, shadow_outcome=""
    ).update(**fields)


def _record_result_sync(
    chat_id: Any,
    turn_id: uuid.UUID,
    fields: Dict[str, Any],
    failure: Optional[str],
) -> int:
    """The health note (success when ``failure`` is None), then the row."""
    from fighthealthinsurance.models import ExternalServiceHealth

    if failure is None:
        ExternalServiceHealth.note_success(chat_shadow.SERVICE)
    else:
        ExternalServiceHealth.note_failure(chat_shadow.SERVICE, failure)
    return _store_sync(chat_id, turn_id, fields)


def _request_seconds() -> float:
    return float(getattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 20))


def job_seconds(request_seconds: float) -> float:
    """The bound on one whole job: both database steps, the requests and
    their grace, and JOB_SPARE_SECONDS."""
    return (
        2 * DB_STEP_SECONDS
        + request_seconds
        + chat_shadow.GRACE_SECONDS
        + JOB_SPARE_SECONDS
    )


async def _score_and_store(
    chat_id: Any,
    turn_id: uuid.UUID,
    message: str,
    reply: str,
    second: Optional[str],
    request_seconds: float,
) -> None:
    # The database steps go through isolated_db rather than
    # database_sync_to_async or the async ORM (CLAUDE.md), on purpose: this
    # task inherits the chat's thread-sensitive executor, and a stuck query
    # there would hold up the chat's own ORM calls (isolated_db says more).
    # The score write's place is reserved before anything is sent, so a job
    # that sends its texts to TypeSafe can always store what comes back.
    slot = isolated_db.reserve(DB_THREAD_NAME, DB_THREAD_LIMIT)
    if slot is None:
        logger.info(
            f"Chat shadow scoring skipped for turn {turn_id}: "
            f"{DB_THREAD_LIMIT} database threads in use"
        )
        return
    try:
        try:
            identifiers = await isolated_db.run_isolated(
                chat_redactions,
                chat_id,
                timeout=DB_STEP_SECONDS,
                name=DB_THREAD_NAME,
                limit=DB_THREAD_LIMIT,
            )
        except Exception as e:
            # No identifier list means nothing is sent: the generic patterns
            # alone are not the redaction this promises.
            logger.warning(
                f"Chat shadow scoring skipped for turn {turn_id}: "
                f"no redactions ({type(e).__name__})"
            )
            return
        result = await chat_shadow.score_turn(
            message,
            reply,
            second,
            identifiers=identifiers,
            timeout_seconds=request_seconds,
        )
        if result is None:
            return
        await isolated_db.run_isolated(
            _record_result_sync,
            chat_id,
            turn_id,
            _shadow_fields(result),
            None if result.outcome == chat_shadow.SCORED else result.failure,
            timeout=DB_STEP_SECONDS,
            name=DB_THREAD_NAME,
            slot=slot,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(
            f"Chat shadow scoring failed for turn {turn_id}: {type(e).__name__}"
        )
    finally:
        # Given back here only when the write never started in it.
        slot.release()


async def _job(
    chat_id: Any,
    turn_id: uuid.UUID,
    message: str,
    reply: str,
    second: Optional[str],
) -> None:
    request_seconds = _request_seconds()
    bound = job_seconds(request_seconds)
    try:
        await asyncio.wait_for(
            _score_and_store(chat_id, turn_id, message, reply, second, request_seconds),
            bound,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Chat shadow scoring for turn {turn_id} stopped after {bound:.0f}s"
        )


def in_flight() -> int:
    return len(_in_flight)


def start(
    *,
    chat_id: Any,
    turn_id: uuid.UUID,
    external_allowed: bool,
    message: Optional[str],
    reply: Optional[str],
    second: Optional[str] = None,
) -> "Optional[asyncio.Task[None]]":
    """Start shadow scoring for one delivered turn in the background.

    ``external_allowed`` must be the person's consent to outside models for
    this chat. Returns the task, or None when nothing was started (consent
    off, the flag or key missing, nothing to score, or MAX_IN_FLIGHT tasks
    or DB_THREAD_LIMIT database threads already running). Never awaits and never raises.
    """
    try:
        if not external_allowed or not chat_shadow.enabled():
            return None
        if not (message or "").strip() or not (reply or "").strip():
            return None
        if (
            len(_in_flight) >= MAX_IN_FLIGHT
            or isolated_db.running(DB_THREAD_NAME) >= DB_THREAD_LIMIT
        ):
            logger.info(
                f"Chat shadow scoring skipped for turn {turn_id}: "
                f"{MAX_IN_FLIGHT} already in flight"
            )
            return None
        task = asyncio.get_running_loop().create_task(
            _job(chat_id, turn_id, message or "", reply or "", second)
        )
        _in_flight.add(task)
        task.add_done_callback(_in_flight.discard)
        return task
    except Exception as e:
        logger.warning(f"Chat shadow scoring not started: {type(e).__name__}")
        return None

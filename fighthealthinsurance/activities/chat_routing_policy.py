"""Temporal activity that writes one chat routing policy row per run.

The work itself is ``ml/chat_policy.compute_and_store_chat_policy``, the
same function the ``compute_chat_policy`` command runs: read ChatTurn
metadata over a window, compute the policy, append one ChatRoutingPolicy
row and prune rows older than 30 days. This wrapper adds:

* The workflow's run id, stored on the row, so a run writes at most one
  row however often this activity is retried or its completion is lost.
* A thread and database connection of its own for that work, so it never
  holds up the appeal activities in the same worker process, and at most
  one such thread per process (see _astore).
* The Temporal conventions shared with the appeal-journey activities:
  schema and validation errors classed as non-retryable, and failures
  reported by exception class name only.

Everything that crosses into workflow history is a number or an id: the
window and the run id in, the new row's id out. No chat text is read here,
and no exception message is logged or raised, since a message could quote
whatever a query touched.
"""

import asyncio
import threading
from typing import Union

from loguru import logger
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance.activities.appeal_journey import _NON_RETRYABLE_ERRORS
from fighthealthinsurance.ml import chat_policy

# The same bounds the compute_chat_policy command accepts.
MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 30 * 24 * 60
# Temporal run ids are UUIDs; the column holds up to this many characters.
MAX_RUN_ID_LENGTH = 64

# On PostgreSQL, each statement of the store (the lookup by run id, the
# ChatTurn read, the insert, the pruning) ends after this long, waiting on
# a lock included.
STORE_STATEMENT_TIMEOUT_SECONDS = 30
# How long an attempt waits for the store's thread. Less than the
# workflow's two-minute start-to-close, so an attempt that stops waiting
# fails with a retryable error of its own. The thread goes on until it
# finishes or a statement timeout ends it, and the run id stops it from
# adding a second row next to a later attempt's.
STORE_WAIT_SECONDS = 100

# Held by the store's thread from before it starts until it ends, so an
# attempt that stopped waiting still counts until its thread is done and
# retries or later runs cannot pile up threads and connections.
_store_running = threading.Lock()


class StoreStillRunning(Exception):
    """An earlier attempt's store is still running in this process.
    Retryable: the next attempt or run finds it done."""


def _store(window_minutes: int, run_id: str) -> int:
    """Compute and store the run's row on the calling thread's own
    connection, then close that thread's connections (it is a new thread
    each time, so nothing else would)."""
    from django.db import connections

    from fighthealthinsurance.models import ChatRoutingPolicy

    try:
        row = chat_policy.compute_and_store_chat_policy(
            window_minutes=window_minutes,
            source=ChatRoutingPolicy.Source.TEMPORAL,
            run_id=run_id,
            statement_timeout_ms=STORE_STATEMENT_TIMEOUT_SECONDS * 1000,
        )
        return int(row.pk)
    finally:
        connections.close_all()


async def _astore(window_minutes: int, run_id: str) -> int:
    """Run _store on a daemon thread of its own and wait up to
    STORE_WAIT_SECONDS for it.

    Unlike the appeal activities, this does not go through
    database_sync_to_async or the native async ORM (CLAUDE.md), and on
    purpose: in the worker process both run every activity's queries on the
    one process-wide thread-sensitive executor, and cancelling an await
    there does not stop the query. A slow ChatTurn scan or a lock wait here
    would then hold that executor, and the appeal activities' reads, writes
    and lease renewals would queue behind it. The store's own thread and
    connection, closed when it is done, keep it off that executor, and the
    statement timeout on PostgreSQL bounds how long each of its statements
    can run.

    That bounds each statement, not the whole store, so an attempt that
    stops waiting can leave its thread running. Only one runs per process:
    while one does, an attempt raises StoreStillRunning without starting
    another.
    """
    if not _store_running.acquire(blocking=False):
        raise StoreStillRunning()
    loop = asyncio.get_running_loop()
    # Settled with the row id or the error as a result, never with an
    # exception, so an attempt that stopped waiting leaves no unretrieved
    # exception (and its message) for asyncio to log.
    done: "asyncio.Future[Union[int, Exception]]" = loop.create_future()

    def settle(outcome: Union[int, Exception]) -> None:
        if not done.done():
            done.set_result(outcome)

    def work() -> None:
        outcome: Union[int, Exception]
        try:
            outcome = _store(window_minutes, run_id)
        except Exception as e:
            outcome = e
        finally:
            _store_running.release()
        try:
            loop.call_soon_threadsafe(settle, outcome)
        except RuntimeError:
            # The event loop closed while the store ran: nobody is waiting.
            pass

    try:
        threading.Thread(target=work, name="fhi-chat-policy-store", daemon=True).start()
    except BaseException:
        _store_running.release()
        raise
    # Shielded, so an attempt that stops waiting (the timeout here, or the
    # activity being cancelled) leaves the thread's result alone.
    outcome = await asyncio.wait_for(asyncio.shield(done), STORE_WAIT_SECONDS)
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


@activity.defn
async def compute_and_store_chat_policy(window_minutes: int, run_id: str) -> int:
    """Compute a chat routing policy from the last ``window_minutes`` of
    chat turns, store it as the ChatRoutingPolicy row of workflow run
    ``run_id`` (source "temporal") and return the row's id. A retry of the
    same run returns the row the run already stored."""
    if (
        isinstance(window_minutes, bool)
        or not isinstance(window_minutes, int)
        or not MIN_WINDOW_MINUTES <= window_minutes <= MAX_WINDOW_MINUTES
    ):
        # A bad input stays bad on every attempt.
        raise ApplicationError(
            "window_minutes out of range", non_retryable=True
        ) from None
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= MAX_RUN_ID_LENGTH:
        raise ApplicationError(
            "run_id missing or too long", non_retryable=True
        ) from None
    try:
        return int(await _astore(window_minutes, run_id))
    except _NON_RETRYABLE_ERRORS as e:
        name = type(e).__name__
        logger.error(f"Chat routing policy not stored: {name} (not retried)")
        raise ApplicationError(
            f"{name} storing the chat routing policy", non_retryable=True
        ) from None
    except Exception as e:
        # Database hiccups, the statement timeout, an attempt that stopped
        # waiting (TimeoutError), an earlier attempt's store still running
        # (StoreStillRunning) and the like: retryable. The schedule's
        # next run is the real retry if the attempts run out.
        name = type(e).__name__
        logger.warning(f"Chat routing policy not stored: {name}")
        raise ApplicationError(f"{name} storing the chat routing policy") from None

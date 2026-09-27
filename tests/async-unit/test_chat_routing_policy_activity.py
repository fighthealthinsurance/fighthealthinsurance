"""Tests for the chat routing policy Temporal activity, through
``ActivityEnvironment`` (in-process, no Temporal server) against seeded
ChatTurn rows.

These live here rather than in tests/temporal so they run in CI, and because
tests/temporal has no database tests: its fax and appeal activity tests call
the real close_old_connections without database access, which a test
database left open by an earlier test would refuse.

The aggregation and the policy rules have their own tests
(tests/sync/test_chat_policy_command.py, tests/async-unit/test_chat_policy.py);
these check the Temporal wrapper: it writes one row marked "temporal" per
workflow run however often an attempt is retried, returns the row id,
rejects a bad input without retrying, reports failures by exception class
name only, and does its database work on a thread and connection of its
own, so a stuck policy query never holds up an appeal activity.
"""

import asyncio
import datetime
import threading
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import ThreadSensitiveContext
from django.db import connections
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone
from loguru import logger
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import appeal_journey as journey_activities
from fighthealthinsurance.activities import chat_routing_policy as policy_activities
from fighthealthinsurance.appeal_journey_core import STATUS_NOT_FOUND
from fighthealthinsurance.ml import chat_policy
from fighthealthinsurance.models import ChatRoutingPolicy, ChatTurn, OngoingChat

_MOD = "fighthealthinsurance.activities.chat_routing_policy"

# Stands in for text an exception message could carry.
_SECRET = "my insulin was denied"

# A workflow run id, the shape Temporal gives them.
_RUN = "0199a0c2-6f4e-7b41-9c1d-2f3e4a5b6c7d"


def _call(model, status="scored", ms=1000, external=False):
    return {
        "model": model,
        "backend": "",
        "external": external,
        "pass": "primary",
        "depth": 0,
        "history": "truncated",
        "variant": "",
        "status": status,
        "error": "",
        "ms": ms,
        "score": 8820.0 if status == "scored" else None,
    }


def _turn(chat, ago, **fields):
    defaults = dict(
        outcome="ok",
        use_external=True,
        backends=["fhi-local", "claude"],
        winner_model="fhi-local",
        winner_external=False,
        calls=[_call("fhi-local"), _call("claude", external=True)],
    )
    defaults.update(fields)
    turn = ChatTurn.objects.create(chat=chat, **defaults)
    ChatTurn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - ago)
    return turn


@pytest.fixture
def seeded(transactional_db):
    chat = OngoingChat.objects.create()
    _turn(chat, datetime.timedelta(seconds=1))
    _turn(chat, datetime.timedelta(minutes=20))
    _turn(
        chat,
        datetime.timedelta(minutes=30),
        outcome="failed",
        winner_model="",
        winner_external=None,
        calls=[_call("fhi-local", "error", ms=200)],
    )
    # Outside a 60-minute window.
    _turn(chat, datetime.timedelta(hours=3))
    return chat


async def _run(window=60, run_id=_RUN):
    return await ActivityEnvironment().run(
        policy_activities.compute_and_store_chat_policy, window, run_id
    )


class _StuckOnce:
    """Stands in for the ChatTurn read. The first call blocks until
    released, like a scan waiting on a lock or a dead connection; later
    calls, and the first once released, read as usual."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._real = chat_policy.aggregate_chat_turns

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait(10)
        return self._real(*args, **kwargs)


class _Finishes:
    """Wraps the activity's _store and records how each call on its thread
    ended, so a test can wait for a store an attempt stopped waiting for."""

    def __init__(self):
        self.outcomes = []
        self._real = policy_activities._store

    def __call__(self, *args):
        try:
            result = self._real(*args)
        except Exception as e:
            self.outcomes.append(e)
            raise
        self.outcomes.append(result)
        return result

    async def wait_for(self, count, timeout=5.0):
        for _ in range(int(timeout / 0.05)):
            if len(self.outcomes) >= count:
                return True
            await asyncio.sleep(0.05)
        return False


# Most database tests run their ORM work inside a ThreadSensitiveContext, so
# the test's own ORM calls use a thread of their own that ends with the test
# and leave no connection open on the shared one (an in-memory test database
# ignores close). The activity's store runs on a thread of its own either way.


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_activity_stores_one_temporal_row_from_the_turns(seeded):
    async with ThreadSensitiveContext():
        before = await ChatRoutingPolicy.objects.acount()
        row_id = await _run(60)
        assert isinstance(row_id, int)
        assert await ChatRoutingPolicy.objects.acount() == before + 1
        row = await ChatRoutingPolicy.objects.aget(pk=row_id)
    assert row.source == ChatRoutingPolicy.Source.TEMPORAL
    assert row.run_id == _RUN
    assert (row.window_minutes, row.turns_considered) == (60, 3)
    # Three turns are far below the minimum: the default routing.
    assert row.reason == "few_turns"
    assert row.external_excluded == []
    assert row.external_delay_seconds == 0.0
    # The newest turn is from this UTC day whenever the test runs.
    assert set(row.calls_today) <= {"fhi-local", "claude"}
    assert row.calls_today.get("claude", 0) >= 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_each_run_appends_and_the_newest_row_is_the_last_one(seeded):
    async with ThreadSensitiveContext():
        first = await _run(60, run_id=str(uuid.uuid4()))
        second = await _run(24 * 60, run_id=str(uuid.uuid4()))
        newest = await ChatRoutingPolicy.objects.order_by("-created_at", "-id").afirst()
    assert second != first
    assert newest.pk == second
    assert (newest.window_minutes, newest.turns_considered) == (24 * 60, 4)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_retried_attempt_returns_the_runs_row_and_writes_no_other(seeded):
    """A retry, or an attempt whose completion Temporal never heard about,
    finds the row its run already stored: it reads no turns and writes and
    prunes nothing."""
    with (
        patch.object(
            chat_policy,
            "aggregate_chat_turns",
            side_effect=chat_policy.aggregate_chat_turns,
        ) as read,
        patch.object(
            chat_policy,
            "prune_old_chat_policies",
            side_effect=chat_policy.prune_old_chat_policies,
        ) as prune,
    ):
        async with ThreadSensitiveContext():
            first = await _run(60)
            again = await _run(60)
            rows = [r async for r in ChatRoutingPolicy.objects.values("pk", "run_id")]
    assert again == first
    assert rows == [{"pk": first, "run_id": _RUN}]
    assert read.call_count == 1
    assert prune.call_count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_pruning_failure_neither_fails_the_attempt_nor_adds_a_row(seeded):
    with patch.object(
        chat_policy,
        "prune_old_chat_policies",
        side_effect=OperationalError("statement timeout"),
    ):
        async with ThreadSensitiveContext():
            first = await _run(60)
            again = await _run(60)
            count = await ChatRoutingPolicy.objects.acount()
    assert again == first
    assert count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_attempt_that_stops_waiting_leaves_one_row_for_its_run(seeded):
    """The first attempt's store is stuck, so the attempt stops waiting and
    fails with a retryable error. Retries while that store still runs fail
    retryably too, without starting a store of their own, so they cannot
    pile up threads and connections. Once the first store ends, the next
    retry returns the row it stored."""
    stuck = _StuckOnce()
    finishes = _Finishes()
    with (
        patch.object(chat_policy, "aggregate_chat_turns", side_effect=stuck),
        patch.object(policy_activities, "_store", side_effect=finishes),
        patch.object(policy_activities, "STORE_WAIT_SECONDS", 0.5),
    ):
        async with ThreadSensitiveContext():
            try:
                with pytest.raises(ApplicationError) as timed_out:
                    await _run(60)
                assert await asyncio.to_thread(stuck.entered.wait, 5)
                still_running = []
                for _ in range(3):
                    with pytest.raises(ApplicationError) as caught:
                        await _run(60)
                    still_running.append(caught.value)
                stores = [
                    t
                    for t in threading.enumerate()
                    if t.name == "fhi-chat-policy-store"
                ]
            finally:
                stuck.release.set()
            for thread in stores:
                await asyncio.to_thread(thread.join, 5)
                assert not thread.is_alive()
            retried = await _run(60)
            rows = [r async for r in ChatRoutingPolicy.objects.values("pk", "run_id")]
    assert not timed_out.value.non_retryable
    assert "TimeoutError" in str(timed_out.value)
    for error in still_running:
        assert not error.non_retryable
        assert "StoreStillRunning" in str(error)
    assert len(stores) == 1
    assert rows == [{"pk": retried, "run_id": _RUN}]
    # The first store wrote the row; the retry found it.
    assert finishes.outcomes == [retried, retried]
    assert stuck.calls == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stuck_policy_query_does_not_hold_up_an_appeal_activity(seeded):
    """The worker process runs the appeal activities' ORM calls on the one
    process-wide thread-sensitive executor (no ThreadSensitiveContext, as
    here). A policy query that hangs must not sit on that executor, where
    the appeal activities' reads, writes and lease renewals would queue
    behind it."""
    stuck = _StuckOnce()
    with patch.object(chat_policy, "aggregate_chat_turns", side_effect=stuck):
        policy = asyncio.ensure_future(_run(60))
        try:
            assert await asyncio.to_thread(stuck.entered.wait, 5)
            # A real appeal activity, for a denial that does not exist: its
            # connection refresh and its lookup both run on that executor.
            status = await asyncio.wait_for(
                ActivityEnvironment().run(
                    journey_activities.precheck_appeal_journey,
                    "hashed",
                    str(uuid.uuid4()),
                ),
                2,
            )
            turns = await asyncio.wait_for(ChatTurn.objects.acount(), 2)
            still_stuck = not stuck.release.is_set() and not policy.done()
        finally:
            stuck.release.set()
        row_id = await asyncio.wait_for(policy, 10)

    assert status == STATUS_NOT_FOUND
    assert turns == 4
    assert still_stuck
    assert isinstance(row_id, int)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_store_runs_in_bounded_transactions_on_its_own_thread(
    seeded, monkeypatch
):
    """Each step (the lookup by run id, the ChatTurn read, the insert, the
    pruning) runs in a transaction whose statements are bounded on
    PostgreSQL, on the store's own thread, and that thread closes its
    connections when it is done."""
    bounded = []
    closed_on = []

    def bound(connection, ms):
        bounded.append((connection.in_atomic_block, ms, threading.get_ident()))

    real_read = chat_policy.aggregate_chat_turns
    read_on = []

    def read(*args, **kwargs):
        read_on.append(threading.get_ident())
        return real_read(*args, **kwargs)

    monkeypatch.setattr(chat_policy, "_bound_statements", bound)
    monkeypatch.setattr(chat_policy, "aggregate_chat_turns", read)
    monkeypatch.setattr(
        connections, "close_all", lambda: closed_on.append(threading.get_ident())
    )
    async with ThreadSensitiveContext():
        row_id = await _run(60)
    here = threading.get_ident()
    assert isinstance(row_id, int)
    assert [(in_atomic, ms) for in_atomic, ms, _ in bounded] == [(True, 30_000)] * 4
    threads = {t for _, _, t in bounded} | set(read_on) | set(closed_on)
    assert len(threads) == 1 and here not in threads
    assert len(closed_on) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("window", [0, -5, 30 * 24 * 60 + 1, True])
async def test_a_bad_window_fails_without_retry_and_stores_nothing(window):
    async with ThreadSensitiveContext():
        with pytest.raises(ApplicationError) as caught:
            await _run(window)
        stored = await ChatRoutingPolicy.objects.acount()
    assert caught.value.non_retryable
    assert stored == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["", "r" * 65, None, 7])
async def test_a_missing_or_bad_run_id_fails_without_retry_and_stores_nothing(
    run_id,
):
    async with ThreadSensitiveContext():
        with pytest.raises(ApplicationError) as caught:
            await _run(60, run_id=run_id)
        stored = await ChatRoutingPolicy.objects.acount()
    assert caught.value.non_retryable
    assert stored == 0


class _Logs:
    def __enter__(self):
        self.lines = []
        self._id = logger.add(lambda m: self.lines.append(str(m)), level="DEBUG")
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)


async def _fail_with(error):
    with (
        patch(f"{_MOD}._astore", AsyncMock(side_effect=error)),
        _Logs() as logs,
    ):
        with pytest.raises(ApplicationError) as caught:
            await _run(60)
    return caught.value, logs.lines


@pytest.mark.asyncio
async def test_a_schema_error_is_not_retried_and_is_reported_by_class_name():
    err, lines = await _fail_with(ProgrammingError(_SECRET))
    assert err.non_retryable
    assert "ProgrammingError" in str(err)
    assert _SECRET not in str(err)
    assert err.__cause__ is None
    assert lines and not any(_SECRET in line for line in lines)


@pytest.mark.asyncio
async def test_a_database_hiccup_is_retried_and_is_reported_by_class_name():
    err, lines = await _fail_with(OperationalError(_SECRET))
    assert not err.non_retryable
    assert "OperationalError" in str(err)
    assert _SECRET not in str(err)
    assert lines and not any(_SECRET in line for line in lines)


@pytest.mark.asyncio
async def test_any_other_error_is_reported_by_class_name_only():
    err, lines = await _fail_with(ValueError(_SECRET))
    assert not err.non_retryable
    assert "ValueError" in str(err)
    assert _SECRET not in str(err)
    assert not any(_SECRET in line for line in lines)


@pytest.mark.asyncio
async def test_an_error_on_the_stores_thread_reaches_the_activity_by_class():
    """An error raised on the store's thread is classed like any other: a
    schema error is not retried, and its message stays out of the error
    and the logs."""
    with (
        patch(f"{_MOD}._store", side_effect=ProgrammingError(_SECRET)),
        _Logs() as logs,
    ):
        with pytest.raises(ApplicationError) as caught:
            await _run(60)
    assert caught.value.non_retryable
    assert "ProgrammingError" in str(caught.value)
    assert _SECRET not in str(caught.value)
    assert not any(_SECRET in line for line in logs.lines)

"""chat/isolated_db.py: database work kept off a chat's thread-sensitive
executor, on a thread with its own connection, bounded by a timeout that
also ends its statements on PostgreSQL."""

import asyncio
import threading
import time

import pytest
from asgiref.sync import ThreadSensitiveContext
from django.db import connections

from fighthealthinsurance.chat import isolated_db
from fighthealthinsurance.models import OngoingChat


class _FakeCursor:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))


class _FakeConnection:
    def __init__(self, vendor):
        self.vendor = vendor
        self.executed = []

    def cursor(self):
        return _FakeCursor(self.executed)


def test_the_statement_timeout_applies_on_postgresql_only():
    postgres = _FakeConnection("postgresql")
    isolated_db.bound_statements(postgres, 5000)
    assert postgres.executed == [
        ("SELECT set_config('statement_timeout', %s, true)", ["5000"])
    ]
    sqlite = _FakeConnection("sqlite")
    isolated_db.bound_statements(sqlite, 5000)
    assert sqlite.executed == []


def _count_chats():
    return OngoingChat.objects.count()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_work_runs_in_a_bounded_transaction_on_its_own_thread(monkeypatch):
    await OngoingChat.objects.acreate()
    seen = {}

    def bound(connection, ms):
        seen["in_transaction"] = connection.in_atomic_block
        seen["ms"] = ms
        seen["thread"] = threading.get_ident()

    closed_on = []
    monkeypatch.setattr(isolated_db, "bound_statements", bound)
    monkeypatch.setattr(
        connections, "close_all", lambda: closed_on.append(threading.get_ident())
    )

    count = await isolated_db.run_isolated(_count_chats, timeout=1.5, name="t-plain")

    assert count == 1
    # The timeout is set inside the work's transaction, on the work's own
    # thread, whose connection is closed afterwards on that thread.
    assert seen["in_transaction"] is True
    assert seen["ms"] == 1500
    assert seen["thread"] != threading.get_ident()
    assert closed_on == [seen["thread"]]
    assert isolated_db.running("t-plain") == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_works_exception_reaches_the_caller():
    def broken():
        raise LookupError("no such row")

    with pytest.raises(LookupError):
        await isolated_db.run_isolated(broken, timeout=2, name="t-raise")
    assert isolated_db.running("t-raise") == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stuck_work_holds_up_neither_its_caller_nor_the_chats_executor():
    """A query stuck behind a lock must not hang its caller past the
    timeout, and must not sit on the chat's thread-sensitive executor where
    the chat's next ORM call would queue behind it."""
    await OngoingChat.objects.acreate()
    entered, release = threading.Event(), threading.Event()

    def stuck():
        entered.set()
        release.wait(10)

    # The socket's own executor, as PerConnectionThreadSensitiveMixin sets up.
    async with ThreadSensitiveContext():
        try:
            started = time.monotonic()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    isolated_db.run_isolated(stuck, timeout=0.2, name="t-stuck"), 5
                )
            waited = time.monotonic() - started
            # The next ORM call on the chat's executor runs straight away.
            count = await asyncio.wait_for(OngoingChat.objects.acount(), 2)
            still_stuck = entered.is_set() and not release.is_set()
            # The thread is still counted while it runs on.
            lingering = isolated_db.running("t-stuck")
        finally:
            release.set()

    assert waited < 2
    assert count == 1
    assert still_stuck
    assert lingering == 1
    for _ in range(100):
        if isolated_db.running("t-stuck") == 0:
            break
        await asyncio.sleep(0.02)
    assert isolated_db.running("t-stuck") == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_limit_counts_threads_whose_callers_stopped_waiting():
    """Threads stuck on a connection that stopped answering outlive their
    callers' waits. With ``limit``, callers that come along meanwhile, all
    at once included, get Busy and start no thread; once the threads end,
    work runs again."""
    release = threading.Event()
    ran = []

    def stuck():
        ran.append(threading.get_ident())
        release.wait(10)

    try:
        for _ in range(2):
            with pytest.raises(asyncio.TimeoutError):
                await isolated_db.run_isolated(
                    stuck, timeout=0.05, name="t-limit", limit=2
                )
        crowd = await asyncio.gather(
            *(
                isolated_db.run_isolated(
                    stuck, timeout=0.05, name="t-limit", limit=2
                )
                for _ in range(5)
            ),
            return_exceptions=True,
        )
        running = isolated_db.running("t-limit")
    finally:
        release.set()

    assert all(isinstance(result, isolated_db.Busy) for result in crowd)
    assert running == 2
    for _ in range(100):
        if isolated_db.running("t-limit") == 0:
            break
        await asyncio.sleep(0.02)
    assert isolated_db.running("t-limit") == 0
    assert len(ran) == 2
    assert (
        await isolated_db.run_isolated(
            lambda: "done", timeout=2, name="t-limit", limit=2
        )
        == "done"
    )

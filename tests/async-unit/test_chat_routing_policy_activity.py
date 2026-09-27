"""Tests for the chat routing policy Temporal activity, through
``ActivityEnvironment`` (in-process, no Temporal server) against seeded
ChatTurn rows.

These live here rather than in tests/temporal so they run in CI, and because
tests/temporal has no database tests: its fax and appeal activity tests call
the real close_old_connections without database access, which a test
database left open by an earlier test would refuse.

The aggregation and the policy rules have their own tests
(tests/sync/test_chat_policy_command.py, tests/async-unit/test_chat_policy.py);
these check the Temporal wrapper: it writes one row marked "temporal",
returns the row id, rejects a bad window without retrying, and reports
failures by exception class name only.
"""

import datetime
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import ThreadSensitiveContext
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone
from loguru import logger
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fighthealthinsurance.activities import chat_routing_policy as policy_activities
from fighthealthinsurance.models import ChatRoutingPolicy, ChatTurn, OngoingChat

_MOD = "fighthealthinsurance.activities.chat_routing_policy"

# Stands in for text an exception message could carry.
_SECRET = "my insulin was denied"


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


# The database tests run their ORM work inside a ThreadSensitiveContext, so
# the activity's database_sync_to_async calls use a thread of their own that
# ends with the test and leave no connection open on the shared one (an
# in-memory test database ignores close).


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_activity_stores_one_temporal_row_from_the_turns(seeded):
    async with ThreadSensitiveContext():
        before = await ChatRoutingPolicy.objects.acount()
        row_id = await ActivityEnvironment().run(
            policy_activities.compute_and_store_chat_policy, 60
        )
        assert isinstance(row_id, int)
        assert await ChatRoutingPolicy.objects.acount() == before + 1
        row = await ChatRoutingPolicy.objects.aget(pk=row_id)
    assert row.source == ChatRoutingPolicy.Source.TEMPORAL
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
        first = await ActivityEnvironment().run(
            policy_activities.compute_and_store_chat_policy, 60
        )
        second = await ActivityEnvironment().run(
            policy_activities.compute_and_store_chat_policy, 24 * 60
        )
        newest = await ChatRoutingPolicy.objects.order_by("-created_at", "-id").afirst()
    assert second != first
    assert newest.pk == second
    assert (newest.window_minutes, newest.turns_considered) == (24 * 60, 4)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("window", [0, -5, 30 * 24 * 60 + 1, True])
async def test_a_bad_window_fails_without_retry_and_stores_nothing(window):
    async with ThreadSensitiveContext():
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(
                policy_activities.compute_and_store_chat_policy, window
            )
        stored = await ChatRoutingPolicy.objects.acount()
    assert caught.value.non_retryable
    assert stored == 0


@pytest.mark.asyncio
async def test_connections_are_refreshed_before_the_read():
    order = []
    with (
        patch(
            f"{_MOD}._aclose_old_connections",
            AsyncMock(side_effect=lambda: order.append("close")),
        ),
        patch(
            f"{_MOD}._astore",
            AsyncMock(side_effect=lambda window: order.append("store") or 5),
        ),
    ):
        assert (
            await ActivityEnvironment().run(
                policy_activities.compute_and_store_chat_policy, 60
            )
            == 5
        )
    assert order == ["close", "store"]


class _Logs:
    def __enter__(self):
        self.lines = []
        self._id = logger.add(lambda m: self.lines.append(str(m)), level="DEBUG")
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)


async def _fail_with(error):
    with (
        patch(f"{_MOD}._aclose_old_connections", AsyncMock()),
        patch(f"{_MOD}._astore", AsyncMock(side_effect=error)),
        _Logs() as logs,
    ):
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(
                policy_activities.compute_and_store_chat_policy, 60
            )
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

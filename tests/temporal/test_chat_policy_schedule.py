"""The ``chat-routing-policy`` Schedule: its shape, and
``ensure_chat_policy_schedule`` keeping it in step with the flag.

Most cases run against a stand-in client, so they check the calls made
(create; already exists then update; flag off then pause). The last test
runs the same function against a local Temporal dev server, because the
time-skipping test server does not host Schedules, to check the server
really ends up with the spec and the paused state the function promises.
"""

import asyncio
import io
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django.core.management import call_command
from django.test import override_settings
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from fighthealthinsurance import temporal_client
from fighthealthinsurance.ml.chat_policy import DEFAULT_WINDOW_MINUTES
from fighthealthinsurance.temporal_client import (
    CHAT_POLICY_SCHEDULE_ID,
    chat_policy_schedule,
    chat_policy_schedule_enabled,
    ensure_chat_policy_schedule,
)
from fighthealthinsurance.workflows.types import ChatRoutingPolicyInput


@override_settings(TEMPORAL_CHAT_POLICY_TASK_QUEUE="q-policy")
def test_the_schedule_runs_the_policy_workflow_once_a_day():
    schedule = chat_policy_schedule()
    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(days=1)
    assert not schedule.spec.calendars and not schedule.spec.cron_expressions
    # An outage never ends in a burst: overlapping runs are skipped and at
    # most one day of missed runs is made up.
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.policy.catchup_window == timedelta(days=1)
    assert schedule.state.paused is False

    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == "ChatRoutingPolicyWorkflow"
    assert action.task_queue == "q-policy"
    assert action.execution_timeout == timedelta(minutes=5)
    # Numbers only: the window, nothing else.
    assert list(action.args) == [
        ChatRoutingPolicyInput(window_minutes=DEFAULT_WINDOW_MINUTES)
    ]


def test_the_input_default_window_matches_the_policy_writer():
    assert ChatRoutingPolicyInput().window_minutes == DEFAULT_WINDOW_MINUTES


@pytest.mark.parametrize(
    "temporal, policy, expected",
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_the_schedule_needs_both_flags(temporal, policy, expected):
    with override_settings(
        TEMPORAL_ENABLED=temporal, TEMPORAL_CHAT_POLICY_ENABLED=policy
    ):
        assert chat_policy_schedule_enabled() is expected


def test_the_schedule_does_not_need_the_journey_flags():
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_CHAT_POLICY_ENABLED=True,
        TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
        TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
    ):
        assert chat_policy_schedule_enabled() is True


class _FakeClient:
    """Records schedule calls. ``exists`` and ``paused`` describe the
    Schedule the server holds before the call."""

    def __init__(self, exists=False, paused=False, pause_error=None):
        self.exists = exists
        self.paused = paused
        self.created = []
        self.updates = []
        self.handle = Mock()
        self.handle.pause = AsyncMock(side_effect=self._pause)
        self.handle.unpause = AsyncMock()
        self.handle.update = AsyncMock(side_effect=self._update)
        self.pause_error = pause_error

    def get_schedule_handle(self, schedule_id):
        assert schedule_id == CHAT_POLICY_SCHEDULE_ID
        return self.handle

    async def create_schedule(self, schedule_id, schedule, **kwargs):
        assert kwargs.get("rpc_timeout") is not None
        if self.exists:
            raise ScheduleAlreadyRunningError()
        self.created.append((schedule_id, schedule))
        self.exists = True
        return self.handle

    async def _pause(self, **kwargs):
        assert kwargs.get("rpc_timeout") is not None
        if self.pause_error is not None:
            raise self.pause_error
        if not self.exists:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        self.paused = True

    async def _update(self, updater, **kwargs):
        assert kwargs.get("rpc_timeout") is not None
        current = Mock()
        current.description.schedule.state = ScheduleState(paused=self.paused)
        update = updater(current)
        assert isinstance(update, ScheduleUpdate)
        self.updates.append(update.schedule)


@override_settings(TEMPORAL_CHAT_POLICY_TASK_QUEUE="q-policy")
def test_enabled_with_no_schedule_creates_it():
    client = _FakeClient(exists=False)
    assert asyncio.run(ensure_chat_policy_schedule(client, enabled=True)) == "created"
    ((schedule_id, schedule),) = client.created
    assert schedule_id == "chat-routing-policy"
    assert isinstance(schedule, Schedule)
    assert schedule.action.task_queue == "q-policy"
    assert schedule == chat_policy_schedule()
    client.handle.update.assert_not_awaited()
    client.handle.pause.assert_not_awaited()


@override_settings(TEMPORAL_CHAT_POLICY_TASK_QUEUE="q-policy-2")
def test_enabled_with_a_schedule_already_there_updates_it():
    client = _FakeClient(exists=True, paused=False)
    assert asyncio.run(ensure_chat_policy_schedule(client, enabled=True)) == "updated"
    assert client.created == []
    (schedule,) = client.updates
    # The spec, action and policy are replaced with today's, so a changed
    # interval or queue reaches a Schedule created by an older release.
    assert schedule == chat_policy_schedule()
    assert schedule.action.task_queue == "q-policy-2"
    client.handle.unpause.assert_not_awaited()
    client.handle.pause.assert_not_awaited()


def test_enabled_with_a_paused_schedule_updates_and_unpauses_it():
    client = _FakeClient(exists=True, paused=True)
    assert asyncio.run(ensure_chat_policy_schedule(client, enabled=True)) == "updated"
    assert len(client.updates) == 1
    client.handle.unpause.assert_awaited_once()
    assert client.handle.unpause.await_args.kwargs.get("rpc_timeout") is not None


def test_disabled_pauses_the_schedule_and_never_creates_one():
    client = _FakeClient(exists=True)
    assert asyncio.run(ensure_chat_policy_schedule(client, enabled=False)) == "paused"
    assert client.paused is True
    assert client.created == [] and client.updates == []


def test_disabled_with_no_schedule_does_nothing():
    client = _FakeClient(exists=False)
    assert asyncio.run(ensure_chat_policy_schedule(client, enabled=False)) == "absent"
    assert client.created == [] and client.updates == []


def test_disabled_with_the_server_failing_raises_for_the_caller_to_log():
    client = _FakeClient(
        exists=True,
        pause_error=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""),
    )
    with pytest.raises(RPCError):
        asyncio.run(ensure_chat_policy_schedule(client, enabled=False))


# --- manage.py ensure_temporal_schedules --------------------------------------


def _command(**flags):
    out = io.StringIO()
    ensure = AsyncMock(return_value="created")
    connect = AsyncMock(return_value=Mock(name="client"))
    with (
        override_settings(**flags),
        patch.object(temporal_client, "ensure_chat_policy_schedule", ensure),
        patch.object(temporal_client, "get_temporal_client", connect),
    ):
        call_command("ensure_temporal_schedules", stdout=out)
    return out.getvalue(), ensure, connect


def test_the_command_does_nothing_with_temporal_off():
    out, ensure, connect = _command(
        TEMPORAL_ENABLED=False, TEMPORAL_CHAT_POLICY_ENABLED=True
    )
    connect.assert_not_awaited()
    ensure.assert_not_awaited()
    assert "TEMPORAL_ENABLED is off" in out


@pytest.mark.parametrize("policy", [True, False])
def test_the_command_keeps_the_schedule_in_step_with_the_flag(policy):
    out, ensure, connect = _command(
        TEMPORAL_ENABLED=True, TEMPORAL_CHAT_POLICY_ENABLED=policy
    )
    connect.assert_awaited_once()
    ensure.assert_awaited_once()
    assert ensure.await_args.kwargs == {"enabled": policy}
    assert "chat-routing-policy: created" in out


# --- Against a real (local dev) server ----------------------------------------


@pytest.mark.asyncio
async def test_against_a_local_server_create_update_pause_and_unpause():
    from temporalio.testing import WorkflowEnvironment

    queue = f"q-policy-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_local() as env:
        client = env.client
        handle = client.get_schedule_handle(CHAT_POLICY_SCHEDULE_ID)
        with override_settings(TEMPORAL_CHAT_POLICY_TASK_QUEUE=queue):
            assert await ensure_chat_policy_schedule(client, enabled=False) == (
                "absent"
            )
            assert await ensure_chat_policy_schedule(client, enabled=True) == (
                "created"
            )
            described = await handle.describe()
            assert described.schedule.spec.intervals[0].every == timedelta(days=1)
            assert described.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
            assert described.schedule.policy.catchup_window == timedelta(days=1)
            action = described.schedule.action
            assert isinstance(action, ScheduleActionStartWorkflow)
            assert action.workflow == "ChatRoutingPolicyWorkflow"
            assert action.task_queue == queue
            assert action.execution_timeout == timedelta(minutes=5)
            assert described.schedule.state.paused is False

            # A second worker starting (or a restart) updates in place.
            assert await ensure_chat_policy_schedule(client, enabled=True) == (
                "updated"
            )
            assert (await handle.describe()).schedule.state.paused is False

            # Flag off, then on again: paused, then running.
            assert await ensure_chat_policy_schedule(client, enabled=False) == (
                "paused"
            )
            assert (await handle.describe()).schedule.state.paused is True
            assert await ensure_chat_policy_schedule(client, enabled=True) == (
                "updated"
            )
            assert (await handle.describe()).schedule.state.paused is False
        await handle.delete()

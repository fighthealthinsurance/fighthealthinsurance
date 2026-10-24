"""The ``plan-documents-sweep`` Schedule: its shape, and
``ensure_plan_documents_schedule`` keeping it in step with
``plan_documents_enabled()``.

Most cases run against a stand-in client, so they check the calls made
(create; already exists then update; flags off then pause). The last test
runs the same function against a local Temporal dev server, because the
time-skipping test server does not host Schedules.
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
from fighthealthinsurance.temporal_client import (
    PLAN_DOCUMENTS_SCHEDULE_ID,
    ensure_plan_documents_schedule,
    plan_documents_schedule,
)
from fighthealthinsurance.workflows.types import PlanDocumentsSweepInput


@override_settings(TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE="q-plan-docs")
def test_the_schedule_runs_the_sweep_once_an_hour():
    schedule = plan_documents_schedule()
    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(hours=1)
    assert not schedule.spec.calendars and not schedule.spec.cron_expressions
    # An outage never ends in a burst: overlapping runs are skipped and at
    # most one hour of missed runs is made up.
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.policy.catchup_window == timedelta(hours=1)
    assert schedule.state.paused is False

    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == "PlanDocumentsSweepWorkflow"
    assert action.task_queue == "q-plan-docs"
    assert action.execution_timeout == timedelta(minutes=30)
    # Numbers only.
    assert list(action.args) == [PlanDocumentsSweepInput()]


class _FakeClient:
    """Records schedule calls. ``exists`` and ``paused`` describe the
    Schedule the server holds before the call."""

    def __init__(self, exists=False, paused=False):
        self.exists = exists
        self.paused = paused
        self.created = []
        self.updates = []
        self.handle = Mock()
        self.handle.pause = AsyncMock(side_effect=self._pause)
        self.handle.unpause = AsyncMock()
        self.handle.update = AsyncMock(side_effect=self._update)

    def get_schedule_handle(self, schedule_id):
        assert schedule_id == PLAN_DOCUMENTS_SCHEDULE_ID
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


@override_settings(TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE="q-plan-docs")
def test_enabled_with_no_schedule_creates_it():
    client = _FakeClient(exists=False)
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=True)) == (
        "created"
    )
    ((schedule_id, schedule),) = client.created
    assert schedule_id == "plan-documents-sweep"
    assert isinstance(schedule, Schedule)
    assert schedule == plan_documents_schedule()


def test_enabled_with_a_paused_schedule_updates_and_unpauses_it():
    client = _FakeClient(exists=True, paused=True)
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=True)) == (
        "updated"
    )
    assert client.updates == [plan_documents_schedule()]
    client.handle.unpause.assert_awaited_once()
    note = client.handle.unpause.await_args.kwargs["note"]
    assert note == "TEMPORAL_PLAN_DOCUMENTS_ENABLED is on"


def test_disabled_pauses_the_schedule_and_never_creates_one():
    client = _FakeClient(exists=True)
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=False)) == (
        "paused"
    )
    assert client.paused is True
    assert client.created == [] and client.updates == []
    note = client.handle.pause.await_args.kwargs["note"]
    assert note == "TEMPORAL_PLAN_DOCUMENTS_ENABLED is off"


def test_disabled_with_no_schedule_does_nothing():
    client = _FakeClient(exists=False)
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=False)) == (
        "absent"
    )
    assert client.created == []


# --- manage.py ensure_temporal_schedules --------------------------------------


@pytest.mark.parametrize(
    "flags, expected",
    [
        (dict(TEMPORAL_PLAN_DOCUMENTS_ENABLED=True, TEMPORAL_PAYLOAD_KEY="k"), True),
        (dict(TEMPORAL_PLAN_DOCUMENTS_ENABLED=True, TEMPORAL_PAYLOAD_KEY=""), False),
        (dict(TEMPORAL_PLAN_DOCUMENTS_ENABLED=False, TEMPORAL_PAYLOAD_KEY="k"), False),
    ],
)
def test_the_command_keeps_both_schedules_in_step(flags, expected):
    out = io.StringIO()
    policy = AsyncMock(return_value="paused")
    plan_documents = AsyncMock(return_value="created")
    with (
        override_settings(
            TEMPORAL_ENABLED=True, TEMPORAL_CHAT_POLICY_ENABLED=False, **flags
        ),
        patch.object(temporal_client, "ensure_chat_policy_schedule", policy),
        patch.object(temporal_client, "ensure_plan_documents_schedule", plan_documents),
        patch.object(
            temporal_client, "get_temporal_client", AsyncMock(return_value=Mock())
        ),
    ):
        call_command("ensure_temporal_schedules", stdout=out)
    assert plan_documents.await_args.kwargs == {"enabled": expected}
    assert policy.await_args.kwargs == {"enabled": False}
    assert "chat-routing-policy: paused" in out.getvalue()
    assert "plan-documents-sweep: created" in out.getvalue()


# --- Against a real (local dev) server ----------------------------------------


@pytest.mark.asyncio
async def test_against_a_local_server_create_pause_and_unpause():
    from temporalio.testing import WorkflowEnvironment

    queue = f"q-plan-docs-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_local() as env:
        client = env.client
        handle = client.get_schedule_handle(PLAN_DOCUMENTS_SCHEDULE_ID)
        with override_settings(TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE=queue):
            assert await ensure_plan_documents_schedule(client, enabled=False) == (
                "absent"
            )
            assert await ensure_plan_documents_schedule(client, enabled=True) == (
                "created"
            )
            described = await handle.describe()
            assert described.schedule.spec.intervals[0].every == timedelta(hours=1)
            assert described.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
            assert described.schedule.policy.catchup_window == timedelta(hours=1)
            action = described.schedule.action
            assert isinstance(action, ScheduleActionStartWorkflow)
            assert action.workflow == "PlanDocumentsSweepWorkflow"
            assert action.task_queue == queue
            assert described.schedule.state.paused is False

            assert await ensure_plan_documents_schedule(client, enabled=False) == (
                "paused"
            )
            assert (await handle.describe()).schedule.state.paused is True
            assert await ensure_plan_documents_schedule(client, enabled=True) == (
                "updated"
            )
            assert (await handle.describe()).schedule.state.paused is False
        await handle.delete()

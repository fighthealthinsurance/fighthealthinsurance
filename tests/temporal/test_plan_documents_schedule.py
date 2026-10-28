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
from cryptography.fernet import Fernet
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from fighthealthinsurance import temporal_client
from fighthealthinsurance.temporal_codec import ENCODING
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

    def __init__(self, exists=False, paused=False, namespace="fhi-research"):
        self.namespace = namespace
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
    assert note == "plan_documents_enabled() is true"


def test_disabled_pauses_the_schedule_and_never_creates_one():
    client = _FakeClient(exists=True)
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=False)) == (
        "paused"
    )
    assert client.paused is True
    assert client.created == [] and client.updates == []
    # The note names the whole condition, not the one flag.
    note = client.handle.pause.await_args.kwargs["note"]
    assert note == (
        "plan_documents_enabled() is false (it needs TEMPORAL_ENABLED, "
        "TEMPORAL_PLAN_DOCUMENTS_ENABLED and a TEMPORAL_RESEARCH_PAYLOAD_KEY "
        "of its own)"
    )


def test_disabled_with_no_schedule_does_nothing():
    client = _FakeClient(exists=False)
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=False)) == (
        "absent"
    )
    assert client.created == []


@pytest.mark.parametrize("enabled", [True, False])
def test_a_client_outside_the_research_namespace_is_refused(enabled):
    """The Schedule and the runs it starts live in the research namespace;
    a client in another one touches nothing."""
    client = _FakeClient(exists=True, namespace="default")
    with pytest.raises(ValueError, match="research namespace"):
        asyncio.run(ensure_plan_documents_schedule(client, enabled=enabled))
    assert client.created == [] and client.updates == []
    client.handle.pause.assert_not_awaited()


@override_settings(TEMPORAL_RESEARCH_NAMESPACE="research-elsewhere")
def test_the_research_namespace_comes_from_its_setting():
    with pytest.raises(ValueError):
        asyncio.run(ensure_plan_documents_schedule(_FakeClient(), enabled=True))
    client = _FakeClient(namespace="research-elsewhere")
    assert asyncio.run(ensure_plan_documents_schedule(client, enabled=True)) == (
        "created"
    )


# --- manage.py ensure_temporal_schedules --------------------------------------


_RESEARCH_KEY = Fernet.generate_key().decode()


@pytest.mark.parametrize(
    "flags, expected",
    [
        (
            dict(
                TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
                TEMPORAL_RESEARCH_PAYLOAD_KEY=_RESEARCH_KEY,
            ),
            True,
        ),
        (
            dict(
                TEMPORAL_PLAN_DOCUMENTS_ENABLED=True, TEMPORAL_RESEARCH_PAYLOAD_KEY=""
            ),
            False,
        ),
        (
            dict(
                TEMPORAL_PLAN_DOCUMENTS_ENABLED=False,
                TEMPORAL_RESEARCH_PAYLOAD_KEY=_RESEARCH_KEY,
            ),
            False,
        ),
    ],
)
def test_the_command_keeps_both_schedules_in_step(flags, expected):
    out = io.StringIO()
    policy = AsyncMock(return_value="paused")
    plan_documents = AsyncMock(return_value="created")
    default = Mock(name="default-client")
    connect = AsyncMock(return_value=default)
    with (
        override_settings(
            TEMPORAL_ENABLED=True,
            TEMPORAL_CHAT_POLICY_ENABLED=False,
            TEMPORAL_PAYLOAD_KEY="",
            **flags,
        ),
        patch.object(temporal_client, "ensure_chat_policy_schedule", policy),
        patch.object(temporal_client, "ensure_plan_documents_schedule", plan_documents),
        patch.object(temporal_client, "get_temporal_client", connect),
    ):
        call_command("ensure_temporal_schedules", stdout=out)
    assert plan_documents.await_args.kwargs == {"enabled": expected}
    assert policy.await_args.kwargs == {"enabled": False}
    # The sweep in the research namespace over the command's one connection;
    # the chat policy stays in the default one.
    (research,) = plan_documents.await_args.args
    assert research.namespace == "fhi-research"
    assert research.service_client is default.service_client
    assert policy.await_args.args == (default,)
    connect.assert_awaited_once_with()
    assert "chat-routing-policy: paused" in out.getvalue()
    assert "plan-documents-sweep: created" in out.getvalue()


@pytest.mark.parametrize("failing", ["policy", "plan_documents"])
def test_the_command_tries_every_schedule_and_fails_if_one_did(failing):
    from django.core.management.base import CommandError

    secret = "text that must not be printed"
    ensures = {
        "policy": AsyncMock(return_value="paused"),
        "plan_documents": AsyncMock(return_value="created"),
    }
    ensures[failing] = AsyncMock(side_effect=RuntimeError(secret))
    out = io.StringIO()
    with (
        override_settings(TEMPORAL_ENABLED=True),
        patch.object(temporal_client, "ensure_chat_policy_schedule", ensures["policy"]),
        patch.object(
            temporal_client, "ensure_plan_documents_schedule", ensures["plan_documents"]
        ),
        patch.object(
            temporal_client, "get_temporal_client", AsyncMock(return_value=Mock())
        ),
    ):
        with pytest.raises(CommandError) as raised:
            call_command("ensure_temporal_schedules", stdout=out)
    for ensure in ensures.values():
        ensure.assert_awaited_once()
    lines = out.getvalue().splitlines()
    expected = {
        "policy": "chat-routing-policy: paused",
        "plan_documents": "plan-documents-sweep: created",
    }
    expected[failing] = expected[failing].split(":")[0] + ": failed (RuntimeError)"
    assert lines == [expected["policy"], expected["plan_documents"]]
    assert secret not in out.getvalue() and secret not in str(raised.value)


# --- Against a real (local dev) server ----------------------------------------


@pytest.mark.asyncio
@override_settings(
    TEMPORAL_RESEARCH_NAMESPACE="fhi-research",
    TEMPORAL_RESEARCH_PAYLOAD_KEY=_RESEARCH_KEY,
    TEMPORAL_PAYLOAD_KEY="",
)
async def test_against_a_local_server_create_pause_and_unpause():
    from temporalio.testing import WorkflowEnvironment

    queue = f"q-plan-docs-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_local(
        dev_server_extra_args=["--namespace", "fhi-research"]
    ) as env:
        client = temporal_client.client_for_profile(
            env.client, temporal_client.RESEARCH_PROFILE
        )
        assert client.namespace == "fhi-research"
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
        # Nothing of it in the default namespace.
        with pytest.raises(RPCError) as missing:
            await env.client.get_schedule_handle(PLAN_DOCUMENTS_SCHEDULE_ID).describe()
        assert missing.value.status == RPCStatusCode.NOT_FOUND
        # Its input is sealed with the research key: the namespace read
        # without that key holds only ciphertext.
        raw = Client(**{**env.client.config(), "namespace": "fhi-research"})
        (sealed,) = (
            await raw.get_schedule_handle(PLAN_DOCUMENTS_SCHEDULE_ID).describe()
        ).schedule.action.args
        assert sealed.metadata["encoding"] == ENCODING
        await handle.delete()

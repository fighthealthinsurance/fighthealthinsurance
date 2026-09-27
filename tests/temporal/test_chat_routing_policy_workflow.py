"""Tests for ``ChatRoutingPolicyWorkflow`` orchestration (activity mocked).

The workflow calls one activity and returns the row id it gets back. These
check the window reaches the activity, the bounded retry, and that the run's
history carries numbers only. The activity itself has its own tests.

Requires the Temporal test server, which ``temporalio`` downloads on first run.
"""

import json
import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.workflows.chat_routing_policy import (
    ChatRoutingPolicyWorkflow,
)
from fighthealthinsurance.workflows.types import ChatRoutingPolicyInput


class _Recorder:
    """A stand-in for the real activity that records each call."""

    def __init__(self, row_id=41, fail_times=0, non_retryable=False):
        self.row_id = row_id
        self.fail_times = fail_times
        self.non_retryable = non_retryable
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="compute_and_store_chat_policy")
        async def compute_and_store_chat_policy(window_minutes: int) -> int:
            rec.calls.append(window_minutes)
            if len(rec.calls) <= rec.fail_times:
                raise ApplicationError(
                    "OperationalError storing the chat routing policy",
                    non_retryable=rec.non_retryable,
                )
            return rec.row_id

        return [compute_and_store_chat_policy]


async def _run(env, rec, window_minutes=90):
    task_queue = str(uuid.uuid4())
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ChatRoutingPolicyWorkflow],
        activities=rec.activities(),
    ):
        handle = await env.client.start_workflow(
            ChatRoutingPolicyWorkflow.run,
            ChatRoutingPolicyInput(window_minutes=window_minutes),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        return handle, await handle.result()


@pytest.mark.asyncio
async def test_a_run_stores_one_policy_and_returns_its_row_id():
    rec = _Recorder(row_id=41)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, rec, window_minutes=90)
    assert result == 41
    assert rec.calls == [90]


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried():
    rec = _Recorder(row_id=7, fail_times=2)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, rec)
    assert result == 7
    assert len(rec.calls) == 3


@pytest.mark.asyncio
async def test_three_failures_fail_the_run_and_leave_the_rest_to_the_schedule():
    """Bounded on purpose: the Schedule's next run is the real retry, and a
    failed run shows in the Temporal UI rather than retrying for ever."""
    rec = _Recorder(fail_times=99)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(WorkflowFailureError):
            await _run(env, rec)
    assert len(rec.calls) == 3


@pytest.mark.asyncio
async def test_a_non_retryable_failure_is_not_retried():
    rec = _Recorder(fail_times=99, non_retryable=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(WorkflowFailureError):
            await _run(env, rec)
    assert len(rec.calls) == 1


def _decoded_payloads(history):
    """Every JSON payload in a history, decoded, with the event it came from."""
    found = []
    for event in history.events:
        for descriptor, value in event.ListFields():
            if not descriptor.name.endswith("_attributes"):
                continue
            for field_name in ("input", "result"):
                if not hasattr(value, field_name):
                    continue
                if not value.HasField(field_name):
                    continue
                for payload in getattr(value, field_name).payloads:
                    found.append((descriptor.name, json.loads(payload.data)))
    return found


@pytest.mark.asyncio
async def test_history_holds_the_window_and_the_row_id_and_nothing_else():
    rec = _Recorder(row_id=41)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle, _ = await _run(env, rec, window_minutes=90)
        history = await handle.fetch_history()
    payloads = _decoded_payloads(history)
    assert payloads == [
        ("workflow_execution_started_event_attributes", {"window_minutes": 90}),
        ("activity_task_scheduled_event_attributes", 90),
        ("activity_task_completed_event_attributes", 41),
        ("workflow_execution_completed_event_attributes", 41),
    ]

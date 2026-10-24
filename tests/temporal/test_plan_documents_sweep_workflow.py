"""Tests for ``PlanDocumentsSweepWorkflow`` orchestration (activities stubbed).

A run sends the due reminders one activity each, never retried, then starts
a reading for each share still waiting, as a child it does not wait for. A
reminder whose row is gone sends nothing, a failed send is not tried again,
and a share whose reading is already running is left to it. History holds
numbers, row ids, booleans, share uuids and counts only.

Requires the Temporal test server, which ``temporalio`` downloads on first run.
"""

import json
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import ParentClosePolicy, WorkflowIdReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.workflows.plan_documents_sweep import (
    PlanDocumentsSweepWorkflow,
)
from fighthealthinsurance.workflows.types import (
    PlanDocumentsSweepInput,
    SharedPlanDocumentsInput,
)

BATCH_A = "5b7d1f0e-6a43-4f8e-9c1d-2e3f4a5b6c7d"
BATCH_B = "6c8e2a1f-7b54-4a9f-8d2e-3f4a5b6c7d8e"


@workflow.defn(name="SharedPlanDocumentsWorkflow")
class _StubReading:
    """Stands in for the reading the sweep starts. A share named in
    ``hold`` stays open until released, like a reading still running."""

    def __init__(self) -> None:
        self._released = False

    @workflow.signal
    def release(self) -> None:
        self._released = True

    @workflow.run
    async def run(self, request: SharedPlanDocumentsInput) -> dict:
        if request.share_batch.startswith("hold-"):
            await workflow.wait_condition(lambda: self._released)
        return {}


class _Stubs:
    """Stand-ins for the sweep's activities that record each call."""

    def __init__(self, due=(1, 2), sent=None, batches=(BATCH_A, BATCH_B), due_fails=False):
        self.due = list(due)
        self.sent = sent or {}
        self.batches = list(batches)
        self.due_fails = due_fails
        self.calls: list = []

    def activities(self):
        stubs = self

        @activity.defn(name="find_due_plan_documents_reminders")
        async def find_due_plan_documents_reminders(limit: int) -> list[int]:
            stubs.calls.append(("due", limit))
            if stubs.due_fails:
                raise ApplicationError("OperationalError for due reminders")
            return stubs.due

        @activity.defn(name="send_plan_documents_reminder")
        async def send_plan_documents_reminder(reminder_id: int) -> bool:
            stubs.calls.append(("send", reminder_id))
            outcome = stubs.sent.get(reminder_id, True)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        @activity.defn(name="find_waiting_share_batches")
        async def find_waiting_share_batches(
            received_minutes: int, limit: int
        ) -> list[str]:
            stubs.calls.append(("batches", received_minutes, limit))
            return stubs.batches

        return [
            find_due_plan_documents_reminders,
            send_plan_documents_reminder,
            find_waiting_share_batches,
        ]


async def _run(env, stubs, request=None, before=None):
    task_queue = str(uuid.uuid4())
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[PlanDocumentsSweepWorkflow, _StubReading],
        activities=stubs.activities(),
    ):
        if before is not None:
            await before(env, task_queue)
        handle = await env.client.start_workflow(
            PlanDocumentsSweepWorkflow.run,
            request or PlanDocumentsSweepInput(),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        return handle, await handle.result()


@pytest.mark.asyncio
async def test_due_reminders_are_sent_then_waiting_shares_are_started():
    stubs = _Stubs()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert stubs.calls == [
        ("due", 50),
        ("send", 1),
        ("send", 2),
        ("batches", 15, 50),
    ]
    assert result == {"reminders_sent": 2, "readings_started": 2}


@pytest.mark.asyncio
async def test_the_run_passes_its_numbers_on():
    stubs = _Stubs(due=[], batches=[])
    request = PlanDocumentsSweepInput(max_reminders=7, max_batches=3, received_minutes=20)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, stubs, request)
    assert stubs.calls == [("due", 7), ("batches", 20, 3)]


@pytest.mark.asyncio
async def test_a_reminder_removed_before_its_send_sends_nothing():
    """The activity claims by deleting the row; a row already gone (removed
    by remove-my-data, or claimed by the Ray loop) means no email."""
    stubs = _Stubs(due=[1, 2], sent={1: False})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert result["reminders_sent"] == 1


@pytest.mark.asyncio
async def test_a_failed_send_is_not_tried_again_and_the_run_goes_on():
    stubs = _Stubs(
        due=[1, 2],
        sent={1: ApplicationError("SMTPException for reminder 1", non_retryable=True)},
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert [c for c in stubs.calls if c[0] == "send"] == [("send", 1), ("send", 2)]
    assert result == {"reminders_sent": 1, "readings_started": 2}


@pytest.mark.asyncio
async def test_a_retryable_send_failure_is_still_sent_once():
    """The send's retry policy is one attempt, whatever the error says."""
    stubs = _Stubs(due=[1], sent={1: ApplicationError("TimeoutError for reminder 1")})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run(env, stubs)
    assert [c for c in stubs.calls if c[0] == "send"] == [("send", 1)]


@pytest.mark.asyncio
async def test_a_failed_reminder_lookup_still_starts_the_readings():
    stubs = _Stubs(due_fails=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert [c for c in stubs.calls if c[0] == "send"] == []
    assert result == {"reminders_sent": 0, "readings_started": 2}


def _child_starts(history):
    return [
        event.start_child_workflow_execution_initiated_event_attributes
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]


@pytest.mark.asyncio
async def test_readings_are_children_the_sweep_does_not_wait_for():
    stubs = _Stubs(due=[], batches=[BATCH_A])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle, _ = await _run(env, stubs)
        history = await handle.fetch_history()
    (start,) = _child_starts(history)
    assert start.workflow_type.name == "SharedPlanDocumentsWorkflow"
    assert start.workflow_id == f"shared-plan-documents-{BATCH_A}"
    assert start.parent_close_policy == ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON


@pytest.mark.asyncio
async def test_a_share_whose_reading_is_running_is_left_to_it():
    held = "hold-" + BATCH_A

    async def a_reading_already_running(env, task_queue):
        await env.client.start_workflow(
            "SharedPlanDocumentsWorkflow",
            SharedPlanDocumentsInput(share_batch=held),
            id=f"shared-plan-documents-{held}",
            task_queue=task_queue,
        )

    stubs = _Stubs(due=[], batches=[held, BATCH_B])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs, before=a_reading_already_running)
        await env.client.get_workflow_handle(f"shared-plan-documents-{held}").signal(
            "release"
        )
    assert result == {"reminders_sent": 0, "readings_started": 1}


@pytest.mark.asyncio
async def test_a_share_whose_earlier_reading_has_closed_is_read_again():
    """Staff "Read again" sets rows back to waiting; the sweep then starts a
    new run under the id of one that has already closed."""

    async def an_earlier_reading_that_closed(env, task_queue):
        earlier = await env.client.start_workflow(
            "SharedPlanDocumentsWorkflow",
            SharedPlanDocumentsInput(share_batch=BATCH_A),
            id=f"shared-plan-documents-{BATCH_A}",
            task_queue=task_queue,
        )
        await earlier.result()

    stubs = _Stubs(due=[], batches=[BATCH_A])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle, result = await _run(env, stubs, before=an_earlier_reading_that_closed)
        history = await handle.fetch_history()
    assert result == {"reminders_sent": 0, "readings_started": 1}
    (start,) = _child_starts(history)
    assert start.workflow_id == f"shared-plan-documents-{BATCH_A}"
    assert (
        start.workflow_id_reuse_policy
        == WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    )


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
async def test_history_holds_numbers_ids_shares_and_counts_and_nothing_else():
    stubs = _Stubs(due=[41], batches=[BATCH_A])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle, _ = await _run(env, stubs)
        history = await handle.fetch_history()
    payloads = [
        (event, value)
        for event, value in _decoded_payloads(history)
        # The child's own completion, if it lands before the sweep closes.
        if event != "child_workflow_execution_completed_event_attributes"
    ]
    assert payloads == [
        (
            "workflow_execution_started_event_attributes",
            {"max_reminders": 50, "max_batches": 50, "received_minutes": 15},
        ),
        ("activity_task_scheduled_event_attributes", 50),
        ("activity_task_completed_event_attributes", [41]),
        ("activity_task_scheduled_event_attributes", 41),
        ("activity_task_completed_event_attributes", True),
        ("activity_task_scheduled_event_attributes", 15),
        ("activity_task_scheduled_event_attributes", 50),
        ("activity_task_completed_event_attributes", [BATCH_A]),
        (
            "start_child_workflow_execution_initiated_event_attributes",
            {"share_batch": BATCH_A},
        ),
        (
            "workflow_execution_completed_event_attributes",
            {"reminders_sent": 1, "readings_started": 1},
        ),
    ]

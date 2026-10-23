"""Tests for ``SharedPlanDocumentsWorkflow`` orchestration (activities stubbed).

The workflow lists a share's rows still waiting, reads each in turn, and
marks a row unreadable when its reading fails on every attempt. A row that
is gone (remove-my-data) is counted as gone and nothing more is done for
it. History holds the share's uuid, row ids and statuses only. The real
activities have their own tests (tests/sync and the activity tests here).

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

from fighthealthinsurance.workflows.shared_plan_documents import (
    SharedPlanDocumentsWorkflow,
)
from fighthealthinsurance.workflows.types import SharedPlanDocumentsInput

BATCH = "5b7d1f0e-6a43-4f8e-9c1d-2e3f4a5b6c7d"
ROW_A = "0c1d2e3f-4a5b-4c6d-8e9f-a0b1c2d3e4f5"
ROW_B = "1d2e3f4a-5b6c-4d7e-9f0a-b1c2d3e4f5a6"


class _Stubs:
    """Stand-ins for the activities that record each call.

    ``statuses`` maps a row id to what reading it returns; an exception
    there is raised on every attempt instead.
    """

    def __init__(self, rows=(ROW_A, ROW_B), statuses=None, list_fails=False):
        self.rows = list(rows)
        self.statuses = statuses or {}
        self.list_fails = list_fails
        self.calls: list = []

    def activities(self):
        stubs = self

        @activity.defn(name="list_shared_plan_documents")
        async def list_shared_plan_documents(share_batch: str) -> list[str]:
            stubs.calls.append(("list", share_batch))
            if stubs.list_fails:
                raise ApplicationError("OperationalError for share batch")
            return stubs.rows

        @activity.defn(name="process_shared_plan_document")
        async def process_shared_plan_document(document_id: str) -> str:
            stubs.calls.append(("process", document_id))
            outcome = stubs.statuses.get(document_id, "ready_for_review")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        @activity.defn(name="mark_shared_plan_document_unreadable")
        async def mark_shared_plan_document_unreadable(document_id: str) -> str:
            stubs.calls.append(("mark", document_id))
            return "unreadable"

        return [
            list_shared_plan_documents,
            process_shared_plan_document,
            mark_shared_plan_document_unreadable,
        ]


async def _run(env, stubs, share_batch=BATCH):
    task_queue = str(uuid.uuid4())
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SharedPlanDocumentsWorkflow],
        activities=stubs.activities(),
    ):
        handle = await env.client.start_workflow(
            SharedPlanDocumentsWorkflow.run,
            SharedPlanDocumentsInput(share_batch=share_batch),
            id=f"shared-plan-documents-{share_batch}",
            task_queue=task_queue,
        )
        return handle, await handle.result()


@pytest.mark.asyncio
async def test_each_row_still_waiting_is_read_in_turn():
    stubs = _Stubs(statuses={ROW_B: "unreadable"})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert stubs.calls == [("list", BATCH), ("process", ROW_A), ("process", ROW_B)]
    assert result == {"ready_for_review": 1, "unreadable": 1}


@pytest.mark.asyncio
async def test_a_share_removed_before_the_run_reads_nothing():
    stubs = _Stubs(rows=[])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert stubs.calls == [("list", BATCH)]
    assert result == {}


@pytest.mark.asyncio
async def test_rows_removed_during_the_run_are_counted_gone_and_nothing_is_written():
    stubs = _Stubs(statuses={ROW_A: "gone", ROW_B: "gone"})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert [call for call in stubs.calls if call[0] == "mark"] == []
    assert result == {"gone": 2}


@pytest.mark.asyncio
async def test_a_reading_that_fails_every_attempt_marks_the_row_unreadable():
    stubs = _Stubs(
        statuses={ROW_A: ApplicationError("FileNotRead for shared plan document")}
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    # Three tries, then the row is marked, and the next row is still read.
    assert stubs.calls == [
        ("list", BATCH),
        ("process", ROW_A),
        ("process", ROW_A),
        ("process", ROW_A),
        ("mark", ROW_A),
        ("process", ROW_B),
    ]
    assert result == {"unreadable": 1, "ready_for_review": 1}


@pytest.mark.asyncio
async def test_a_reading_that_cannot_succeed_is_tried_once_then_marked():
    stubs = _Stubs(
        statuses={
            ROW_A: ApplicationError(
                "ProgrammingError for shared plan document", non_retryable=True
            )
        },
        rows=[ROW_A],
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, result = await _run(env, stubs)
    assert stubs.calls == [("list", BATCH), ("process", ROW_A), ("mark", ROW_A)]
    assert result == {"unreadable": 1}


@pytest.mark.asyncio
async def test_a_listing_that_keeps_failing_fails_the_run_for_the_sweep_to_retry():
    stubs = _Stubs(list_fails=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(WorkflowFailureError):
            await _run(env, stubs)
    assert stubs.calls == [("list", BATCH)] * 5


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
async def test_history_holds_the_share_the_row_ids_and_statuses_and_nothing_else():
    stubs = _Stubs(statuses={ROW_B: "gone"})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle, _ = await _run(env, stubs)
        history = await handle.fetch_history()
    assert _decoded_payloads(history) == [
        ("workflow_execution_started_event_attributes", {"share_batch": BATCH}),
        ("activity_task_scheduled_event_attributes", BATCH),
        ("activity_task_completed_event_attributes", [ROW_A, ROW_B]),
        ("activity_task_scheduled_event_attributes", ROW_A),
        ("activity_task_completed_event_attributes", "ready_for_review"),
        ("activity_task_scheduled_event_attributes", ROW_B),
        ("activity_task_completed_event_attributes", "gone"),
        (
            "workflow_execution_completed_event_attributes",
            {"ready_for_review": 1, "gone": 1},
        ),
    ]

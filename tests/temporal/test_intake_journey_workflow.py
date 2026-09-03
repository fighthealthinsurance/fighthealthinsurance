"""Tests for ``IntakeJourneyWorkflow`` orchestration (activities mocked).

Timer behavior runs under the time-skipping environment. The child
generation workflow is replaced by a recording stub registered under the
same name.
"""

import uuid

import pytest

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.workflows.intake_journey import IntakeJourneyWorkflow
from fighthealthinsurance.workflows.types import GenerateAppealInput, IntakeJourneyInput


@workflow.defn(name="GenerateAppealWorkflow")
class _StubGenerateAppeal:
    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        return 3


class _Recorder:
    def __init__(self):
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="send_abandonment_nudge")
        async def send_abandonment_nudge(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append(("nudge", denial_uuid))
            return True

        @activity.defn(name="close_incomplete_journey")
        async def close_incomplete_journey(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append(("close", denial_uuid))
            return True

        return [send_abandonment_nudge, close_incomplete_journey]


async def _start(env, rec, *, contact_opt_in):
    task_queue = str(uuid.uuid4())
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[IntakeJourneyWorkflow, _StubGenerateAppeal],
        activities=rec.activities(),
    )
    handle = await env.client.start_workflow(
        IntakeJourneyWorkflow.run,
        IntakeJourneyInput(
            hashed_email="h", denial_uuid="u", contact_opt_in=contact_opt_in
        ),
        id=str(uuid.uuid4()),
        task_queue=task_queue,
    )
    return worker, handle


@pytest.mark.asyncio
async def test_completion_runs_generation_child_and_skips_nudge():
    rec = _Recorder()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec, contact_opt_in=True)
        async with worker:
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            result = await handle.result()
    assert result == "completed"
    assert rec.calls == []  # completed before any timer: no nudge, no close


@pytest.mark.asyncio
async def test_abandonment_nudges_once_then_closes():
    rec = _Recorder()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec, contact_opt_in=True)
        async with worker:
            result = await handle.result()  # time-skipping runs out both timers
    assert result == "abandoned"
    assert rec.calls == [("nudge", "u"), ("close", "u")]


@pytest.mark.asyncio
async def test_no_opt_in_means_no_nudge():
    rec = _Recorder()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec, contact_opt_in=False)
        async with worker:
            result = await handle.result()
    assert result == "abandoned"
    assert rec.calls == [("close", "u")]


@pytest.mark.asyncio
async def test_query_reports_progress_and_late_completion_generates():
    rec = _Recorder()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec, contact_opt_in=False)
        async with worker:
            await handle.signal(IntakeJourneyWorkflow.step_reached, "health-history")
            state = await handle.query(IntakeJourneyWorkflow.journey_state)
            assert state == {"step": "health-history", "completed": False}
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            result = await handle.result()
    assert result == "completed"
    assert rec.calls == []

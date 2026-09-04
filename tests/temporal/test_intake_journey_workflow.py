"""Tests for ``IntakeJourneyWorkflow`` orchestration (activities mocked).

Timer behavior runs under the time-skipping environment. The child
generation workflow is replaced by a recording stub registered under the
same name.
"""

import asyncio
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


# --- post-collision reconciliation ---------------------------------------


@workflow.defn(name="GenerateAppealWorkflow")
class _BlockingGenerateAppeal:
    """A generation run that holds generate-appeal-{uuid} until released;
    ``release(n)`` closes it returning n drafts."""

    def __init__(self) -> None:
        self._result: int = -1

    @workflow.signal
    def release(self, count: int) -> None:
        self._result = count

    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        await workflow.wait_condition(lambda: self._result >= 0)
        return self._result


class _ReconcileRecorder(_Recorder):
    """Recorder plus the postcondition activity, answering from a script."""

    def __init__(self, postcondition_answers):
        super().__init__()
        self.answers = list(postcondition_answers)

    def activities(self):
        rec = self
        base = super().activities()

        @activity.defn(name="check_generation_postcondition")
        async def check_generation_postcondition(
            hashed_email: str, denial_uuid: str
        ) -> bool:
            rec.calls.append(("postcondition", denial_uuid))
            return rec.answers.pop(0) if len(rec.answers) > 1 else rec.answers[0]

        return base + [check_generation_postcondition]


async def _start_colliding(env, rec):
    """A standalone generation already holds the child id; then intake."""
    task_queue = str(uuid.uuid4())
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[IntakeJourneyWorkflow, _BlockingGenerateAppeal],
        activities=rec.activities(),
    )
    standalone = await env.client.start_workflow(
        _BlockingGenerateAppeal.run,
        GenerateAppealInput(hashed_email="h", denial_uuid="u"),
        id="generate-appeal-u",
        task_queue=task_queue,
    )
    handle = await env.client.start_workflow(
        IntakeJourneyWorkflow.run,
        IntakeJourneyInput(hashed_email="h", denial_uuid="u", contact_opt_in=True),
        id=str(uuid.uuid4()),
        task_queue=task_queue,
    )
    return worker, standalone, handle


@pytest.mark.asyncio
async def test_collision_with_satisfied_postcondition_completes():
    """The standalone run delivered: the postcondition says so and intake
    completes without ever starting its own child."""
    rec = _ReconcileRecorder([True])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, standalone, handle = await _start_colliding(env, rec)
        async with worker:
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            result = await handle.result()
    assert result == "completed"
    assert ("postcondition", "u") in rec.calls


@pytest.mark.asyncio
async def test_collision_then_standalone_dies_short_intake_retakes():
    """The standalone closes with zero drafts; the postcondition stays
    unmet; intake retakes ownership (its own child now starts) and
    completes -- instead of having closed 'completed' on the collision."""
    rec = _ReconcileRecorder([False])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, standalone, handle = await _start_colliding(env, rec)
        async with worker:
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            # Let intake collide and enter reconciliation, then the
            # standalone dies short of the target.
            await asyncio.sleep(1)
            await standalone.signal(_BlockingGenerateAppeal.release, 0)
            await standalone.result()
            # Intake's next attempt (after its backoff timer, which only
            # advances when we skip time) starts its OWN child under the
            # freed id -- the blocking stub again; release it with a full
            # result.
            from datetime import timedelta as _td

            standalone_run = (await standalone.describe()).run_id
            child = env.client.get_workflow_handle("generate-appeal-u")
            for _ in range(20):
                await env.sleep(_td(minutes=11))
                await asyncio.sleep(0.2)
                if (await child.describe()).run_id != standalone_run:
                    break
            await child.signal(_BlockingGenerateAppeal.release, 3)
            result = await handle.result()
    assert result == "completed"
    assert rec.calls.count(("postcondition", "u")) >= 1


@pytest.mark.asyncio
async def test_collision_never_resolved_defers_after_the_window():
    """Postcondition never met and the standalone never closes: the journey
    gives up after the reconciliation window as 'deferred', never
    'completed'."""
    rec = _ReconcileRecorder([False])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, standalone, handle = await _start_colliding(env, rec)
        async with worker:
            await handle.signal(IntakeJourneyWorkflow.form_completed)
            result = await handle.result()
    assert result == "deferred"
    checks = rec.calls.count(("postcondition", "u"))
    # Backoff 30s doubling to a 10m cap over a 24h window: ~148 iterations,
    # and the final clamped sleep exits BEFORE another check -- so the
    # count is bounded, never one-extra past the deadline (review).
    assert 1 < checks <= 150

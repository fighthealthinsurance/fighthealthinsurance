"""``AssistantAppealWorkflow`` orchestration, activities and child stubbed."""

import asyncio
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.workflows.assistant_appeal import AssistantAppealWorkflow
from fighthealthinsurance.workflows.types import AssistantAppealInput, GenerateAppealInput


@workflow.defn(name="GenerateAppealWorkflow")
class _StubGenerateAppeal:
    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        return 3


class _Recorder:
    def __init__(
        self, *, found=True, questions=0, finish="ready", satisfied=True, started=True, fail=()
    ):
        self.started = started
        self.fail = set(fail)
        self.found = found
        self.questions = questions
        self.finish = finish
        self.satisfied = satisfied
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="read_letter")
        async def read_letter(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append("read")
            if "read" in rec.fail:
                raise ApplicationError("out of attempts", non_retryable=True)
            return rec.found

        @activity.defn(name="ask_questions")
        async def ask_questions(hashed_email: str, denial_uuid: str) -> int:
            rec.calls.append("ask")
            if "ask" in rec.fail:
                raise ApplicationError("out of attempts", non_retryable=True)
            return rec.questions

        @activity.defn(name="start_drafting")
        async def start_drafting(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append("start")
            if "start" in rec.fail:
                raise ApplicationError("out of attempts", non_retryable=True)
            return rec.started

        @activity.defn(name="finish_drafts")
        async def finish_drafts(hashed_email: str, denial_uuid: str) -> str:
            rec.calls.append("finish")
            return rec.finish

        @activity.defn(name="mark_draft_status")
        async def mark_draft_status(hashed_email: str, denial_uuid: str, status: str) -> bool:
            rec.calls.append(f"mark:{status}")
            return True

        @activity.defn(name="check_generation_postcondition")
        async def check_generation_postcondition(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append("check")
            return rec.satisfied

        return [
            read_letter,
            ask_questions,
            start_drafting,
            finish_drafts,
            mark_draft_status,
            check_generation_postcondition,
        ]


async def _start(env, rec, *, denial_uuid="u"):
    task_queue = str(uuid.uuid4())
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[AssistantAppealWorkflow, _StubGenerateAppeal],
        activities=rec.activities(),
    )
    handle = await env.client.start_workflow(
        AssistantAppealWorkflow.run,
        AssistantAppealInput(hashed_email="h", denial_uuid=denial_uuid),
        id=str(uuid.uuid4()),
        task_queue=task_queue,
    )
    return worker, handle


@pytest.mark.asyncio
async def test_no_questions_means_drafting_starts_at_once():
    rec = _Recorder(questions=0)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            assert await handle.result() == "ready"
    assert rec.calls == ["read", "ask", "start", "finish"]


@pytest.mark.asyncio
async def test_questions_wait_for_the_answers_signal():
    rec = _Recorder(questions=2)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            await asyncio.sleep(0.5)
            assert rec.calls == ["read", "ask"]
            assert (await handle.query(AssistantAppealWorkflow.draft_state)) == {
                "answered": False
            }
            await handle.signal(AssistantAppealWorkflow.answers_filed)
            assert await handle.result() == "ready"
    assert rec.calls == ["read", "ask", "start", "finish"]


@pytest.mark.asyncio
async def test_unanswered_questions_expire_after_a_day_without_drafting():
    rec = _Recorder(questions=1)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            assert await handle.result() == "expired"
    assert rec.calls == ["read", "ask", "mark:expired"]


@pytest.mark.asyncio
async def test_a_case_that_is_gone_ends_at_once():
    rec = _Recorder(found=False)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            assert await handle.result() == "not_found"
    assert rec.calls == ["read"]


@pytest.mark.asyncio
async def test_a_failed_model_step_goes_on_without_it():
    rec = _Recorder(fail={"read", "ask"})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            assert await handle.result() == "ready"
    assert rec.calls == ["read", "ask", "start", "finish"]


@pytest.mark.asyncio
async def test_a_draft_gone_before_drafting_starts_no_generation():
    rec = _Recorder(started=False)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            assert await handle.result() == "not_found"
    assert rec.calls == ["read", "ask", "start"]


@pytest.mark.asyncio
async def test_a_failed_bookkeeping_step_marks_the_draft_stopped():
    rec = _Recorder(fail={"start"})
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await _start(env, rec)
        async with worker:
            with pytest.raises(WorkflowFailureError):
                await handle.result()
    assert rec.calls == ["read", "ask", "start", "mark:stopped"]


@workflow.defn(name="GenerateAppealWorkflow")
class _BlockingGenerateAppeal:
    """A standalone generation that holds generate-appeal-{uuid} open."""

    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        await workflow.wait_condition(lambda: False)
        return 0


@workflow.defn(name="GenerateAppealWorkflow")
class _Failing:
    @workflow.run
    async def run(self, journey: GenerateAppealInput) -> int:
        raise ApplicationError("out of attempts", non_retryable=True)


async def _with_generation_taken(rec):
    """Run the workflow while a standalone generation holds the child id."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = str(uuid.uuid4())
        worker = Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AssistantAppealWorkflow, _BlockingGenerateAppeal],
            activities=rec.activities(),
        )
        async with worker:
            await env.client.start_workflow(
                _BlockingGenerateAppeal.run,
                GenerateAppealInput(hashed_email="h", denial_uuid="taken"),
                id="generate-appeal-taken",
                task_queue=task_queue,
            )
            handle = await env.client.start_workflow(
                AssistantAppealWorkflow.run,
                AssistantAppealInput(hashed_email="h", denial_uuid="taken"),
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
            return await asyncio.wait_for(handle.result(), timeout=120)


@pytest.mark.asyncio
async def test_a_generation_already_running_is_checked_not_assumed():
    rec = _Recorder(finish="on_site")
    assert await _with_generation_taken(rec) == "on_site"
    assert rec.calls == ["read", "ask", "start", "check", "finish"]


@pytest.mark.asyncio
async def test_a_generation_that_never_delivers_is_given_up_inside_the_window():
    rec = _Recorder(satisfied=False, finish="stopped")
    assert await _with_generation_taken(rec) == "stopped"
    assert rec.calls[:4] == ["read", "ask", "start", "check"]
    assert rec.calls[-1] == "finish"
    assert set(rec.calls[3:-1]) == {"check"}


@pytest.mark.asyncio
async def test_a_failed_generation_still_reports_what_landed():
    rec = _Recorder(finish="stopped")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = str(uuid.uuid4())
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AssistantAppealWorkflow, _Failing],
            activities=rec.activities(),
        ):
            handle = await env.client.start_workflow(
                AssistantAppealWorkflow.run,
                AssistantAppealInput(hashed_email="h", denial_uuid="u"),
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
            assert await handle.result() == "stopped"
    assert rec.calls == ["read", "ask", "start", "finish"]


@pytest.mark.asyncio
async def test_the_client_starts_and_signals_the_workflow_by_its_id():
    from django.conf import settings

    from fighthealthinsurance.temporal_client import (
        assistant_appeal_workflow_id,
        signal_assistant_answers_filed,
        start_assistant_appeal_workflow,
    )

    rec = _Recorder(questions=1)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=settings.TEMPORAL_APPEAL_TASK_QUEUE,
            workflows=[AssistantAppealWorkflow, _StubGenerateAppeal],
            activities=rec.activities(),
        ):
            started = await start_assistant_appeal_workflow("h", "u1", client=env.client)
            assert started == assistant_appeal_workflow_id("u1")
            await signal_assistant_answers_filed("u1", client=env.client)
            handle = env.client.get_workflow_handle(started)
            assert await handle.result() == "ready"
    assert rec.calls == ["read", "ask", "start", "finish"]

"""``AssistantAppealWorkflow`` orchestration, activities and child stubbed."""

import asyncio
import uuid

import pytest
from temporalio import activity, workflow
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
    def __init__(self, *, found=True, questions=0, finish="ready"):
        self.found = found
        self.questions = questions
        self.finish = finish
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="read_letter")
        async def read_letter(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append("read")
            return rec.found

        @activity.defn(name="ask_questions")
        async def ask_questions(hashed_email: str, denial_uuid: str) -> int:
            rec.calls.append("ask")
            return rec.questions

        @activity.defn(name="start_drafting")
        async def start_drafting(hashed_email: str, denial_uuid: str) -> bool:
            rec.calls.append("start")
            return True

        @activity.defn(name="finish_drafts")
        async def finish_drafts(hashed_email: str, denial_uuid: str) -> str:
            rec.calls.append("finish")
            return rec.finish

        @activity.defn(name="mark_draft_status")
        async def mark_draft_status(hashed_email: str, denial_uuid: str, status: str) -> bool:
            rec.calls.append(f"mark:{status}")
            return True

        return [read_letter, ask_questions, start_drafting, finish_drafts, mark_draft_status]


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
async def test_a_generation_the_site_already_owns_means_on_site():
    rec = _Recorder()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = str(uuid.uuid4())
        worker = Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AssistantAppealWorkflow, _StubGenerateAppeal],
            activities=rec.activities(),
        )
        async with worker:
            # A standalone generation holding the deterministic child id.
            await env.client.start_workflow(
                _StubGenerateAppeal.run,
                GenerateAppealInput(hashed_email="h", denial_uuid="taken"),
                id="generate-appeal-taken",
                task_queue=str(uuid.uuid4()),
            )
            handle = await env.client.start_workflow(
                AssistantAppealWorkflow.run,
                AssistantAppealInput(hashed_email="h", denial_uuid="taken"),
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
            assert await handle.result() == "on_site"
    assert rec.calls == ["read", "ask", "start", "mark:on_site"]

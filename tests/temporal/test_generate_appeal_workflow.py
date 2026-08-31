"""Tests for ``GenerateAppealWorkflow`` orchestration (activities mocked).

These validate the durable-workflow control flow -- which activities run, in
what order, with what retry behavior -- without touching the database or a
model backend. The underlying ``appeal_journey_core`` logic has its own
activity-level tests.

Requires the Temporal test server, which ``temporalio`` downloads on first run.
"""

import uuid

import pytest

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.appeal_journey_core import (
    STATUS_ALREADY_HAS_APPEALS,
    STATUS_NO_DENIAL_TEXT,
    STATUS_NOT_FOUND,
    STATUS_OK,
)
from fighthealthinsurance.workflows.generate_appeal import GenerateAppealWorkflow
from fighthealthinsurance.workflows.types import GenerateAppealInput


class _Recorder:
    """Builds mock journey activities that record calls and return canned values."""

    def __init__(
        self,
        precheck_status: str = STATUS_OK,
        stored: int = 3,
        precheck_fail_times: int = 0,
        generate_fail_times: int = 0,
    ):
        self.precheck_status = precheck_status
        self.stored = stored
        self.precheck_fail_times = precheck_fail_times
        self.generate_fail_times = generate_fail_times
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="precheck_appeal_journey")
        async def precheck_appeal_journey(hashed_email: str, denial_uuid: str) -> str:
            rec.calls.append(("precheck", hashed_email, denial_uuid))
            precheck_count = sum(1 for c in rec.calls if c[0] == "precheck")
            if precheck_count <= rec.precheck_fail_times:
                raise ApplicationError("simulated transient precheck failure")
            return rec.precheck_status

        @activity.defn(name="generate_and_store_appeals")
        async def generate_and_store_appeals(
            hashed_email: str, denial_uuid: str
        ) -> int:
            rec.calls.append(("generate", hashed_email, denial_uuid))
            generate_count = sum(1 for c in rec.calls if c[0] == "generate")
            if generate_count <= rec.generate_fail_times:
                raise ApplicationError("simulated transient generation failure")
            return rec.stored

        return [precheck_appeal_journey, generate_and_store_appeals]


async def _run(env: WorkflowEnvironment, rec: _Recorder):
    task_queue = str(uuid.uuid4())
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[GenerateAppealWorkflow],
        activities=rec.activities(),
    ):
        return await env.client.execute_workflow(
            GenerateAppealWorkflow.run,
            GenerateAppealInput(hashed_email="h", denial_uuid="u"),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )


@pytest.mark.asyncio
async def test_ok_path_generates_and_reports_stored_count():
    rec = _Recorder(precheck_status=STATUS_OK, stored=3)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result == 3
    assert [c[0] for c in rec.calls] == ["precheck", "generate"]


@pytest.mark.asyncio
async def test_not_found_stops_without_generating():
    rec = _Recorder(precheck_status=STATUS_NOT_FOUND)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result == 0
    assert [c[0] for c in rec.calls] == ["precheck"]


@pytest.mark.asyncio
async def test_no_denial_text_stops_without_generating():
    rec = _Recorder(precheck_status=STATUS_NO_DENIAL_TEXT)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result == 0
    assert [c[0] for c in rec.calls] == ["precheck"]


@pytest.mark.asyncio
async def test_already_has_appeals_is_idempotent_noop():
    """A duplicate dispatch (or post-crash retry) with drafts already stored
    must end cleanly without generating more."""
    rec = _Recorder(precheck_status=STATUS_ALREADY_HAS_APPEALS)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result == 0
    assert [c[0] for c in rec.calls] == ["precheck"]


@pytest.mark.asyncio
async def test_precheck_retries_through_transient_failure():
    """Precheck retries forever (capped backoff): a bounded retry running out
    would orphan the journey before any drafts were attempted."""
    rec = _Recorder(precheck_status=STATUS_OK, stored=1, precheck_fail_times=3)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(env, rec)
    assert result == 1
    assert sum(1 for c in rec.calls if c[0] == "precheck") >= 4
    assert [c[0] for c in rec.calls if c[0] == "generate"] == ["generate"]


@pytest.mark.asyncio
async def test_generation_retries_transient_failure_then_succeeds():
    """Unlike the fax vendor send, generation stores idempotently (the core
    dedupes against existing drafts), so transient backend failures retry."""
    rec = _Recorder(precheck_status=STATUS_OK, stored=2, generate_fail_times=2)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(env, rec)
    assert result == 2
    assert sum(1 for c in rec.calls if c[0] == "generate") == 3


@pytest.mark.asyncio
async def test_generation_exhausting_retries_fails_the_workflow():
    """Three failed attempts surface as a failed workflow -- visible in the
    Temporal UI -- rather than silently reporting zero drafts."""
    rec = _Recorder(precheck_status=STATUS_OK, generate_fail_times=99)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(WorkflowFailureError):
            await _run(env, rec)
    assert sum(1 for c in rec.calls if c[0] == "generate") == 3

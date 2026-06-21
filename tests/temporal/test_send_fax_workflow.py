"""Tests for ``SendFaxWorkflow`` orchestration (activities mocked).

These validate the durable-workflow control flow -- which activities run, in
what order, with what arguments -- without touching the database or the fax
vendor. The underlying ``fax_send_core`` logic is covered by the (now
core-delegating) Ray actor tests in ``tests/sync-actor/test_fax_actor.py``.

Requires the Temporal test server, which ``temporalio`` downloads on first run.
"""

import uuid

import pytest

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.fax_status import (
    STATUS_ALREADY_SENT,
    STATUS_MISSING_DENIAL,
    STATUS_MISSING_DESTINATION,
    STATUS_OK,
)
from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow
from fighthealthinsurance.workflows.types import SendFaxInput


class _Recorder:
    """Builds mock fax activities that record calls and return canned values."""

    def __init__(self, precheck_status: str = STATUS_OK, send_result: bool = True):
        self.precheck_status = precheck_status
        self.send_result = send_result
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="precheck_fax")
        async def precheck_fax(hashed_email: str, fax_uuid: str) -> str:
            rec.calls.append(("precheck", hashed_email, fax_uuid))
            return rec.precheck_status

        @activity.defn(name="send_fax_via_vendor")
        async def send_fax_via_vendor(hashed_email: str, fax_uuid: str) -> bool:
            rec.calls.append(("send", hashed_email, fax_uuid))
            return rec.send_result

        @activity.defn(name="finalize_fax")
        async def finalize_fax(
            hashed_email: str,
            fax_uuid: str,
            fax_success: bool,
            missing_destination: bool,
        ) -> bool:
            rec.calls.append(("finalize", fax_success, missing_destination))
            return True

        return [precheck_fax, send_fax_via_vendor, finalize_fax]


async def _run(env: WorkflowEnvironment, rec: _Recorder, *, delay_send: bool = False):
    task_queue = str(uuid.uuid4())
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SendFaxWorkflow],
        activities=rec.activities(),
    ):
        return await env.client.execute_workflow(
            SendFaxWorkflow.run,
            SendFaxInput(hashed_email="h", fax_uuid="u", delay_send=delay_send),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )


@pytest.mark.asyncio
async def test_ok_path_sends_and_finalizes():
    rec = _Recorder(precheck_status=STATUS_OK, send_result=True)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result is True
    assert [c[0] for c in rec.calls] == ["precheck", "send", "finalize"]
    assert rec.calls[-1] == ("finalize", True, False)


@pytest.mark.asyncio
async def test_send_failure_is_finalized_as_failure():
    rec = _Recorder(precheck_status=STATUS_OK, send_result=False)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result is False
    assert rec.calls[-1] == ("finalize", False, False)


@pytest.mark.asyncio
async def test_missing_denial_stops_without_send():
    rec = _Recorder(precheck_status=STATUS_MISSING_DENIAL)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result is False
    assert [c[0] for c in rec.calls] == ["precheck"]


@pytest.mark.asyncio
async def test_missing_destination_finalizes_without_send():
    rec = _Recorder(precheck_status=STATUS_MISSING_DESTINATION)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result is False
    assert [c[0] for c in rec.calls] == ["precheck", "finalize"]
    assert rec.calls[-1] == ("finalize", False, True)


@pytest.mark.asyncio
async def test_already_sent_is_noop():
    rec = _Recorder(precheck_status=STATUS_ALREADY_SENT)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result is False
    assert [c[0] for c in rec.calls] == ["precheck"]


@pytest.mark.asyncio
async def test_delay_send_waits_then_sends():
    """The 1h delay timer is auto-skipped by the time-skipping environment."""
    rec = _Recorder(precheck_status=STATUS_OK, send_result=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(env, rec, delay_send=True)
    assert result is True
    assert [c[0] for c in rec.calls] == ["precheck", "send", "finalize"]

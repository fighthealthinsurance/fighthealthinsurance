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
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fighthealthinsurance.fax_status import (
    STATUS_ALREADY_SENT,
    STATUS_MISSING_DENIAL,
    STATUS_MISSING_DESTINATION,
    STATUS_NOT_FOUND,
    STATUS_OK,
)
from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow
from fighthealthinsurance.workflows.types import SendFaxInput


class _Recorder:
    """Builds mock fax activities that record calls and return canned values."""

    def __init__(
        self,
        precheck_status: str = STATUS_OK,
        send_result: bool = True,
        send_raises: bool = False,
        finalize_fail_times: int = 0,
        precheck_fail_times: int = 0,
    ):
        self.precheck_status = precheck_status
        self.send_result = send_result
        self.send_raises = send_raises
        self.finalize_fail_times = finalize_fail_times
        self.precheck_fail_times = precheck_fail_times
        self.calls: list = []

    def activities(self):
        rec = self

        @activity.defn(name="precheck_fax")
        async def precheck_fax(hashed_email: str, fax_uuid: str) -> str:
            rec.calls.append(("precheck", hashed_email, fax_uuid))
            precheck_count = sum(1 for c in rec.calls if c[0] == "precheck")
            if precheck_count <= rec.precheck_fail_times:
                raise ApplicationError("simulated transient precheck failure")
            return rec.precheck_status

        @activity.defn(name="send_fax_via_vendor")
        async def send_fax_via_vendor(hashed_email: str, fax_uuid: str) -> bool:
            rec.calls.append(("send", hashed_email, fax_uuid))
            if rec.send_raises:
                raise ApplicationError("simulated vendor failure")
            return rec.send_result

        @activity.defn(name="release_send_claim")
        async def release_send_claim(hashed_email: str, fax_uuid: str) -> bool:
            rec.calls.append(("release", hashed_email, fax_uuid))
            return True

        @activity.defn(name="finalize_fax")
        async def finalize_fax(
            hashed_email: str,
            fax_uuid: str,
            fax_success: bool,
            missing_destination: bool,
        ) -> bool:
            rec.calls.append(("finalize", fax_success, missing_destination))
            finalize_count = sum(1 for c in rec.calls if c[0] == "finalize")
            if finalize_count <= rec.finalize_fail_times:
                raise ApplicationError("simulated transient finalize failure")
            return True

        return [precheck_fax, send_fax_via_vendor, release_send_claim, finalize_fax]


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
async def test_send_failure_is_finalized_then_claim_released():
    rec = _Recorder(precheck_status=STATUS_OK, send_result=False)
    async with await WorkflowEnvironment.start_local() as env:
        result = await _run(env, rec)
    assert result is False
    # User notification (finalize) comes first; the durable claim release runs
    # after, so a failed send can always be explicitly re-sent.
    assert [c[0] for c in rec.calls] == ["precheck", "send", "finalize", "release"]
    assert rec.calls[2] == ("finalize", False, False)


def test_timeout_type_classification():
    from unittest.mock import Mock as _Mock

    from temporalio.exceptions import TimeoutError as TemporalTimeoutError, TimeoutType

    from fighthealthinsurance.workflows.send_fax import _timeout_type

    hb = TemporalTimeoutError(
        "t", type=TimeoutType.HEARTBEAT, last_heartbeat_details=[]
    )
    assert _timeout_type(_Mock(cause=hb)) == TimeoutType.HEARTBEAT
    stc = TemporalTimeoutError(
        "t", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]
    )
    assert _timeout_type(_Mock(cause=stc)) == TimeoutType.START_TO_CLOSE
    assert _timeout_type(_Mock(cause=RuntimeError("x"))) is None
    assert _timeout_type(RuntimeError("no cause attr")) is None


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
async def test_not_found_stops_without_send_or_finalize():
    rec = _Recorder(precheck_status=STATUS_NOT_FOUND)
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


@pytest.mark.asyncio
async def test_send_raising_is_finalized_as_failure_without_concurrent_retry():
    """A raising send is NOT retried, then recorded as a failed send.

    The vendor activity is a thread Temporal cannot cancel, so retrying after
    a timeout could run a second attempt *concurrently* with the still-running
    first one and double-fax. It runs at most once per attempt
    (maximum_attempts=1), and the workflow only re-attempts on a HEARTBEAT
    timeout (worker process dead, thread provably gone). A vendor error gets
    no retry; it is finalized as a failed send (user notified) and the claim
    is then released so an explicit resend can transmit.
    """
    rec = _Recorder(precheck_status=STATUS_OK, send_raises=True)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(env, rec)
    assert result is False
    assert rec.calls.count(("send", "h", "u")) == 1
    assert [c[0] for c in rec.calls] == ["precheck", "send", "finalize", "release"]
    assert rec.calls[2] == ("finalize", False, False)


@pytest.mark.asyncio
async def test_precheck_retries_through_transient_failure_then_sends():
    """A precheck that fails transiently is retried, not orphaned.

    A bounded precheck retry that ran out would fail the whole workflow before
    the fax was ever sent -- no finalize, no notification, and (with the Ray
    delayed sweep gated off under Temporal) nothing to retry it. So precheck
    retries through the outage and the fax still goes out.
    """
    rec = _Recorder(precheck_status=STATUS_OK, send_result=True, precheck_fail_times=3)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(env, rec)
    assert result is True
    assert rec.calls.count(("precheck", "h", "u")) >= 4
    assert [c[0] for c in rec.calls if c[0] in ("send", "finalize")] == [
        "send",
        "finalize",
    ]


@pytest.mark.asyncio
async def test_finalize_retries_through_transient_failures():
    """Post-send bookkeeping must survive transient outages, not fail the run.

    Finalize fails 4 times then succeeds -- past the old maximum_attempts=3
    limit, which would have failed the workflow with the fax already delivered
    but recorded as unsent.
    """
    rec = _Recorder(precheck_status=STATUS_OK, send_result=True, finalize_fail_times=4)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run(env, rec)
    assert result is True
    finalize_calls = [c for c in rec.calls if c[0] == "finalize"]
    assert len(finalize_calls) == 5
    assert finalize_calls[-1] == ("finalize", True, False)

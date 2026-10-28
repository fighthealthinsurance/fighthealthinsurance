"""Tests for the Temporal dispatch helpers' error handling.

``WorkflowAlreadyStartedError`` means Temporal already owns the fax; treating
it as a failure would make callers fall back to a concurrent Ray send for the
same fax (double-fax risk), so it must map to "handled".
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from django.test import override_settings

from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment

from fighthealthinsurance.helpers.fax_helpers import _astart_fax_sends
from fighthealthinsurance.temporal_client import (
    dispatch_fax_send,
    start_send_fax_workflow,
)


@override_settings(TEMPORAL_ENABLED=True)
@patch("fighthealthinsurance.temporal_client.start_send_fax_workflow")
def test_dispatch_treats_already_started_as_handled(mock_start):
    mock_start.side_effect = AsyncMock(
        side_effect=WorkflowAlreadyStartedError("send-fax-u", "SendFaxWorkflow")
    )
    assert dispatch_fax_send("h", "u") is True


@override_settings(TEMPORAL_ENABLED=True)
@patch("fighthealthinsurance.temporal_client.start_send_fax_workflow")
def test_dispatch_returns_false_on_other_errors(mock_start):
    mock_start.side_effect = AsyncMock(side_effect=RuntimeError("server down"))
    assert dispatch_fax_send("h", "u") is False


@override_settings(TEMPORAL_ENABLED=False)
def test_dispatch_returns_false_when_disabled():
    assert dispatch_fax_send("h", "u") is False


@pytest.mark.asyncio
async def test_the_server_refuses_a_second_send_for_a_fax_with_an_open_run():
    """The workflow id is the fax's own, so while a run for a fax is open a
    plain start fails as already started (dispatch_fax_send counts that as
    handled): the views and the stranded-fax reconcile can't open a second
    send of one fax. No worker polls the queue, so the first run stays open."""
    async with await WorkflowEnvironment.start_local() as env:
        with (
            patch(
                "fighthealthinsurance.temporal_client.get_temporal_client",
                AsyncMock(return_value=env.client),
            ),
            override_settings(TEMPORAL_TASK_QUEUE=f"no-worker-{uuid.uuid4()}"),
        ):
            await start_send_fax_workflow("h", "u")
            with pytest.raises(WorkflowAlreadyStartedError):
                await start_send_fax_workflow("h", "u")


@pytest.mark.asyncio
async def test_a_stranded_fax_found_on_two_passes_gets_one_run():
    """The reconcile's second pass, while the first pass's run is still
    open, counts it and starts nothing new."""
    fax = ("h", f"u-{uuid.uuid4()}")
    async with await WorkflowEnvironment.start_local() as env:
        with (
            patch(
                "fighthealthinsurance.temporal_client.get_temporal_client",
                AsyncMock(return_value=env.client),
            ),
            override_settings(TEMPORAL_TASK_QUEUE=f"no-worker-{uuid.uuid4()}"),
        ):
            assert await _astart_fax_sends([fax]) == 1
            handle = env.client.get_workflow_handle(f"send-fax-{fax[1]}")
            first_run = (await handle.describe()).run_id
            assert await _astart_fax_sends([fax]) == 1
            assert (await handle.describe()).run_id == first_run

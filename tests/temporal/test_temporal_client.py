"""Tests for the Temporal dispatch helpers' error handling.

``WorkflowAlreadyStartedError`` means Temporal already owns the fax; treating
it as a failure would make callers fall back to a concurrent Ray send for the
same fax (double-fax risk), so it must map to "handled".
"""

from unittest.mock import AsyncMock, patch

from django.test import override_settings

from temporalio.exceptions import WorkflowAlreadyStartedError

from fighthealthinsurance.temporal_client import dispatch_fax_send


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

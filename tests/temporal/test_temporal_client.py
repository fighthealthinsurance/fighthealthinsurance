"""Tests for the Temporal dispatch helpers' error handling.

``WorkflowAlreadyStartedError`` means Temporal already owns the fax; treating
it as a failure would make callers fall back to a concurrent Ray send for the
same fax (double-fax risk), so it must map to "handled".
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.test import override_settings

from temporalio.exceptions import WorkflowAlreadyStartedError

from fighthealthinsurance.temporal_client import (
    dispatch_fax_send,
    plan_documents_enabled,
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


# --- Shared plan documents ----------------------------------------------------


@pytest.mark.parametrize(
    "temporal, flag, key, expected",
    [
        (True, True, "k", True),
        (True, True, "", False),
        (True, False, "k", False),
        (False, True, "k", False),
    ],
)
def test_plan_documents_need_temporal_their_flag_and_the_payload_key(
    temporal, flag, key, expected
):
    with override_settings(
        TEMPORAL_ENABLED=temporal,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=flag,
        TEMPORAL_PAYLOAD_KEY=key,
    ):
        assert plan_documents_enabled() is expected


def test_plan_documents_do_not_need_the_journey_or_policy_flags():
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_PAYLOAD_KEY="k",
        TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
        TEMPORAL_CHAT_POLICY_ENABLED=False,
    ):
        assert plan_documents_enabled() is True

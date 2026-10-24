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
    dispatch_shared_plan_documents,
    plan_documents_enabled,
)
from fighthealthinsurance.workflows.types import SharedPlanDocumentsInput


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


# --- Starting a share's reading from the request ------------------------------

_ON = dict(
    TEMPORAL_ENABLED=True,
    TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
    TEMPORAL_PAYLOAD_KEY="k",
    TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE="q-plan-docs",
)
BATCH = "5b7d1f0e-6a43-4f8e-9c1d-2e3f4a5b6c7d"
_CLIENT = "fighthealthinsurance.temporal_client"


def test_a_share_is_not_started_with_the_flags_off():
    connect = AsyncMock()
    with (
        override_settings(**{**_ON, "TEMPORAL_PAYLOAD_KEY": ""}),
        patch(f"{_CLIENT}.get_temporal_client", connect),
    ):
        assert dispatch_shared_plan_documents(BATCH) is False
    connect.assert_not_awaited()


def test_a_share_starts_one_reading_named_by_its_batch_on_its_queue():
    client = AsyncMock()
    client.start_workflow.return_value.id = f"shared-plan-documents-{BATCH}"
    with (
        override_settings(**_ON),
        patch(f"{_CLIENT}.get_temporal_client", AsyncMock(return_value=client)),
    ):
        assert dispatch_shared_plan_documents(BATCH) is True
    (args, kwargs) = client.start_workflow.await_args
    assert args == ("SharedPlanDocumentsWorkflow", SharedPlanDocumentsInput(BATCH))
    assert kwargs["id"] == f"shared-plan-documents-{BATCH}"
    assert kwargs["task_queue"] == "q-plan-docs"
    assert kwargs["rpc_timeout"] is not None


def test_a_reading_already_running_counts_as_started():
    start = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            f"shared-plan-documents-{BATCH}", "SharedPlanDocumentsWorkflow"
        )
    )
    with (
        override_settings(**_ON),
        patch(f"{_CLIENT}.start_shared_plan_documents_workflow", start),
    ):
        assert dispatch_shared_plan_documents(BATCH) is True


def test_a_failed_start_returns_false_and_logs_only_its_class():
    from loguru import logger

    secret = "Rebecca Lee Crumpler"
    lines: list = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        with (
            override_settings(**_ON),
            patch(
                f"{_CLIENT}.start_shared_plan_documents_workflow",
                AsyncMock(side_effect=RuntimeError(secret)),
            ),
        ):
            assert dispatch_shared_plan_documents(BATCH) is False
    finally:
        logger.remove(sink)
    assert any("RuntimeError" in line for line in lines)
    assert not any(secret in line for line in lines)


def test_a_slow_start_gives_up_quickly():
    import asyncio
    import time

    async def hangs(share_batch):
        await asyncio.sleep(30)

    started = time.monotonic()
    with (
        override_settings(**_ON),
        patch(f"{_CLIENT}.PLAN_DOCUMENTS_START_WAIT_SECONDS", 0.05),
        patch(f"{_CLIENT}.start_shared_plan_documents_workflow", hangs),
    ):
        assert dispatch_shared_plan_documents(BATCH) is False
    assert time.monotonic() - started < 5

"""Tests for the Temporal dispatch helpers' error handling.

``WorkflowAlreadyStartedError`` means Temporal already owns the fax; treating
it as a failure would make callers fall back to a concurrent Ray send for the
same fax (double-fax risk), so it must map to "handled".
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from django.test import override_settings

from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment

from fighthealthinsurance.helpers.fax_helpers import _astart_fax_sends
from fighthealthinsurance.temporal_client import (
    RESEARCH_PROFILE,
    dispatch_fax_send,
    dispatch_shared_plan_documents,
    plan_documents_enabled,
    research_payload_key_problem,
    start_send_fax_workflow,
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


# --- Shared plan documents ----------------------------------------------------

_KEY = Fernet.generate_key().decode()
_IN_BOTH_KEY = Fernet.generate_key().decode()
_OLD_KEY = Fernet.generate_key().decode()


@pytest.mark.parametrize(
    "temporal, flag, key, expected",
    [
        (True, True, _KEY, True),
        (True, True, "", False),
        (True, True, " , ,", False),
        (True, False, _KEY, False),
        (False, True, _KEY, False),
    ],
)
def test_plan_documents_need_temporal_their_flag_and_the_research_key(
    temporal, flag, key, expected
):
    with override_settings(
        TEMPORAL_ENABLED=temporal,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=flag,
        TEMPORAL_RESEARCH_PAYLOAD_KEY=key,
        TEMPORAL_PAYLOAD_KEY="",
    ):
        assert plan_documents_enabled() is expected


def test_the_shared_payload_key_alone_does_not_turn_plan_documents_on():
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_PAYLOAD_KEY="shared",
        TEMPORAL_RESEARCH_PAYLOAD_KEY="",
    ):
        assert plan_documents_enabled() is False


@pytest.mark.parametrize(
    "research",
    [
        _IN_BOTH_KEY,
        f"{_KEY},{_IN_BOTH_KEY}",
        f" {_IN_BOTH_KEY} ,{_KEY}",
        # The same key bytes, written without the base64 padding.
        _IN_BOTH_KEY.rstrip("=") + "==",
    ],
)
def test_a_research_key_the_shared_setting_also_holds_keeps_plan_documents_off(
    research,
):
    """Either key would then read both namespaces' history."""
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_PAYLOAD_KEY=f"{_OLD_KEY},{_IN_BOTH_KEY}",
        TEMPORAL_RESEARCH_PAYLOAD_KEY=research,
    ):
        assert plan_documents_enabled() is False


@pytest.mark.parametrize("research", ["k", f"{_KEY},not-a-key", "a" * 44])
def test_a_malformed_research_key_keeps_plan_documents_off(research):
    """Off everywhere, so the Ray email loop keeps sending reminders."""
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_PAYLOAD_KEY="",
        TEMPORAL_RESEARCH_PAYLOAD_KEY=research,
    ):
        assert plan_documents_enabled() is False
        assert "not a Fernet key" in research_payload_key_problem()


def test_a_malformed_shared_key_does_not_turn_plan_documents_off():
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_PAYLOAD_KEY="k",
        TEMPORAL_RESEARCH_PAYLOAD_KEY=_KEY,
    ):
        assert plan_documents_enabled() is True


def test_a_research_namespace_that_is_the_default_one_keeps_plan_documents_off():
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_PAYLOAD_KEY=_IN_BOTH_KEY,
        TEMPORAL_RESEARCH_PAYLOAD_KEY=_KEY,
        TEMPORAL_NAMESPACE="default",
        TEMPORAL_RESEARCH_NAMESPACE="default",
    ):
        assert plan_documents_enabled() is False
        assert "TEMPORAL_RESEARCH_NAMESPACE" in research_payload_key_problem()


def test_plan_documents_do_not_need_the_journey_or_policy_flags():
    with override_settings(
        TEMPORAL_ENABLED=True,
        TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
        TEMPORAL_RESEARCH_PAYLOAD_KEY=_KEY,
        TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
        TEMPORAL_CHAT_POLICY_ENABLED=False,
    ):
        assert plan_documents_enabled() is True


# --- Starting a share's reading from the request ------------------------------

_ON = dict(
    TEMPORAL_ENABLED=True,
    TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
    TEMPORAL_RESEARCH_PAYLOAD_KEY=_KEY,
    TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE="q-plan-docs",
)
BATCH = "5b7d1f0e-6a43-4f8e-9c1d-2e3f4a5b6c7d"
_CLIENT = "fighthealthinsurance.temporal_client"


@pytest.mark.parametrize(
    "off",
    [
        dict(TEMPORAL_RESEARCH_PAYLOAD_KEY="", TEMPORAL_PAYLOAD_KEY="k"),
        dict(TEMPORAL_PLAN_DOCUMENTS_ENABLED=False),
        dict(TEMPORAL_ENABLED=False),
    ],
)
def test_a_share_is_not_started_with_the_flags_off(off):
    connect = AsyncMock()
    with (
        override_settings(**{**_ON, **off}),
        patch(f"{_CLIENT}.get_temporal_client", connect),
    ):
        assert dispatch_shared_plan_documents(BATCH) is False
    connect.assert_not_awaited()


def test_a_share_starts_one_reading_named_by_its_batch_on_its_queue():
    client = AsyncMock()
    client.start_workflow.return_value.id = f"shared-plan-documents-{BATCH}"
    connect = AsyncMock(return_value=client)
    with (
        override_settings(**_ON),
        patch(f"{_CLIENT}.get_temporal_client", connect),
    ):
        assert dispatch_shared_plan_documents(BATCH) is True
    # In the research namespace, with its key.
    assert connect.await_args.kwargs == {"profile": RESEARCH_PROFILE}
    args, kwargs = client.start_workflow.await_args
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


# --- Namespaces and payload keys per profile ----------------------------------

_SHARED_KEY = Fernet.generate_key().decode()
_RESEARCH_KEY = Fernet.generate_key().decode()
_PROFILES = dict(
    TEMPORAL_HOST="test-host",
    TEMPORAL_NAMESPACE="default",
    TEMPORAL_RESEARCH_NAMESPACE="fhi-research",
    TEMPORAL_PAYLOAD_KEY=_SHARED_KEY,
    TEMPORAL_RESEARCH_PAYLOAD_KEY=_RESEARCH_KEY,
    TEMPORAL_TLS=False,
)


def _connect_kwargs(**call):
    """``Client.connect``'s kwargs from ``get_temporal_client(**call)``."""
    import asyncio

    from fighthealthinsurance.temporal_client import get_temporal_client

    connect = AsyncMock(return_value="client")
    with patch("temporalio.client.Client.connect", connect):
        assert asyncio.run(get_temporal_client(**call)) == "client"
    (host,) = connect.await_args.args
    assert host == "test-host"
    return connect.await_args.kwargs


def _round_trip(encode_with, decode_with):
    """Encode one payload with one converter's codec and decode it with
    another's; returns the decoded data or raises."""
    import asyncio

    from temporalio.api.common.v1 import Payload

    async def run():
        (sealed,) = await encode_with.payload_codec.encode(
            [Payload(metadata={"encoding": b"json/plain"}, data=b'"x"')]
        )
        (opened,) = await decode_with.payload_codec.decode([sealed])
        return opened.data

    return asyncio.run(run())


@override_settings(**_PROFILES)
def test_the_default_profile_connects_as_before():
    """Default namespace and the shared key only, whatever the research
    settings say."""
    kwargs = _connect_kwargs()
    assert kwargs["namespace"] == "default"
    assert kwargs["tls"] is False
    assert "runtime" not in kwargs
    shared = kwargs["data_converter"]
    assert _round_trip(shared, shared) == b'"x"'
    with override_settings(TEMPORAL_PAYLOAD_KEY=""):
        assert "data_converter" not in _connect_kwargs()


@override_settings(**_PROFILES)
def test_the_research_profile_connects_to_its_namespace_with_its_key():
    runtime = object()
    kwargs = _connect_kwargs(profile=RESEARCH_PROFILE, runtime=runtime)
    assert kwargs["namespace"] == "fhi-research"
    assert kwargs["runtime"] is runtime
    research = kwargs["data_converter"]
    assert _round_trip(research, research) == b'"x"'


@override_settings(**_PROFILES)
def test_neither_key_reads_the_other_namespaces_payloads():
    from cryptography.fernet import InvalidToken

    shared = _connect_kwargs()["data_converter"]
    research = _connect_kwargs(profile=RESEARCH_PROFILE)["data_converter"]
    with pytest.raises(InvalidToken):
        _round_trip(research, shared)
    with pytest.raises(InvalidToken):
        _round_trip(shared, research)


@override_settings(**_PROFILES)
def test_a_research_client_over_a_connection_opens_no_new_one():
    """The worker and ensure_temporal_schedules reach the research namespace
    over the connection they already hold."""
    from unittest.mock import Mock

    from temporalio.client import Client

    from fighthealthinsurance.temporal_client import (
        _profile_data_converter,
        client_for_profile,
    )

    base = Client(
        Mock(), namespace="default", data_converter=_profile_data_converter("default")
    )
    connect = AsyncMock()
    with patch("temporalio.client.Client.connect", connect):
        research = client_for_profile(base, RESEARCH_PROFILE)
    connect.assert_not_awaited()
    assert research.namespace == "fhi-research"
    assert research.service_client is base.service_client
    assert base.namespace == "default"
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        _round_trip(research.data_converter, base.data_converter)
    assert _round_trip(research.data_converter, research.data_converter) == b'"x"'


@override_settings(**{**_PROFILES, "TEMPORAL_RESEARCH_PAYLOAD_KEY": ""})
def test_a_research_client_never_borrows_the_shared_key():
    from unittest.mock import Mock

    from temporalio.client import Client

    from fighthealthinsurance.temporal_client import (
        _profile_data_converter,
        client_for_profile,
    )

    base = Client(Mock(), data_converter=_profile_data_converter("default"))
    assert base.data_converter.payload_codec is not None
    research = client_for_profile(base, RESEARCH_PROFILE)
    assert research.data_converter.payload_codec is None
    assert "data_converter" not in _connect_kwargs(profile=RESEARCH_PROFILE)


@pytest.mark.parametrize(
    "outcome, expected",
    [(None, True), ("NOT_FOUND", False)],
)
def test_namespace_exists_reads_describe(outcome, expected):
    import asyncio
    from unittest.mock import Mock

    from temporalio.service import RPCError, RPCStatusCode

    from fighthealthinsurance.temporal_client import namespace_exists

    client = Mock(namespace="fhi-research")
    client.workflow_service.describe_namespace = AsyncMock(
        side_effect=(
            RPCError("missing", RPCStatusCode.NOT_FOUND, b"") if outcome else None
        )
    )
    assert asyncio.run(namespace_exists(client)) is expected
    (request,) = client.workflow_service.describe_namespace.await_args.args
    assert request.namespace == "fhi-research"
    assert client.workflow_service.describe_namespace.await_args.kwargs["timeout"]


def test_namespace_exists_raises_on_other_failures():
    import asyncio
    from unittest.mock import Mock

    from temporalio.service import RPCError, RPCStatusCode

    from fighthealthinsurance.temporal_client import namespace_exists

    client = Mock(namespace="fhi-research")
    client.workflow_service.describe_namespace = AsyncMock(
        side_effect=RPCError("down", RPCStatusCode.UNAVAILABLE, b"")
    )
    with pytest.raises(RPCError):
        asyncio.run(namespace_exists(client))

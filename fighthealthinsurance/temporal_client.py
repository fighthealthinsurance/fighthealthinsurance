"""Temporal client connection, fax-dispatch helpers, the chat policy
Schedule, and starting the reading of shared plan documents with the
Schedule that sweeps them. Shared plan documents run in a namespace of
their own (RESEARCH_PROFILE); everything else runs in the default one.

All ``temporalio`` imports are done lazily inside the functions so that simply
importing this module (e.g. from ``fax_helpers``) does not require ``temporalio``
or touch the network. When ``settings.TEMPORAL_ENABLED`` is False -- the default
-- :func:`dispatch_fax_send` short-circuits before importing anything Temporal,
leaving the existing Ray path entirely untouched.
"""

import asyncio
import base64
from datetime import timedelta
from typing import Any, Callable, Optional

from asgiref.sync import async_to_sync
from django.conf import settings

from loguru import logger

# Upper bound on how long a *blocking* dispatch waits for a workflow result
# before returning (the run keeps executing server-side). Without this a
# synchronous caller that attaches to a sleeping ``delay_send`` run would block
# for the full 1-hour delay timer -- long enough to exhaust a web worker.
_RESULT_WAIT_SECONDS = 15 * 60

# Per-RPC bound on every intake-journey start/signal/describe call: the SDK
# default is no timeout, and the outbox relay must never let one call eat
# its lifetime (belt and suspenders with the caller's asyncio.wait_for).
INTAKE_RPC_TIMEOUT_SECONDS = 15.0

# The longest a person's own request waits on Temporal for the chat path,
# connecting included: Agree on the terms page, and the answers an assistant
# sends. Past it the request goes on as it does when Temporal is down.
ASSISTANT_REQUEST_WAIT_SECONDS = 10.0


# --- Namespaces ---------------------------------------------------------------

DEFAULT_PROFILE = "default"
RESEARCH_PROFILE = "research"

# The settings naming each profile's namespace and payload key.
TEMPORAL_PROFILES: dict[str, tuple[str, str]] = {
    DEFAULT_PROFILE: ("TEMPORAL_NAMESPACE", "TEMPORAL_PAYLOAD_KEY"),
    RESEARCH_PROFILE: ("TEMPORAL_RESEARCH_NAMESPACE", "TEMPORAL_RESEARCH_PAYLOAD_KEY"),
}


def temporal_namespace(profile: str = DEFAULT_PROFILE) -> str:
    """The Temporal namespace a profile's workflows run in."""
    namespace_setting, _ = TEMPORAL_PROFILES[profile]
    return str(getattr(settings, namespace_setting))


def _profile_data_converter(profile: str) -> Any:
    """The profile's encrypting data converter, built from its own key
    setting alone, or None when that setting is unset."""
    _, key_setting = TEMPORAL_PROFILES[profile]
    payload_key = getattr(settings, key_setting, "")
    if not payload_key:
        return None
    return _encrypting_data_converter(payload_key, key_setting)


async def get_temporal_client(
    runtime: Any = None, profile: str = DEFAULT_PROFILE
) -> Any:
    """Connect a Temporal client using Django settings.

    Supports a plain connection (local dev server), server-side TLS, and mTLS
    with client cert/key files for a self-hosted cluster.

    ``runtime`` is the SDK ``temporalio.runtime.Runtime`` carrying telemetry
    config; only the worker process passes one (it hosts the Prometheus
    metrics endpoint). Web/Ray callers leave it None and get the default.

    ``profile`` picks the namespace and payload key (TEMPORAL_PROFILES); the
    default is the one every caller had before there were profiles. To reach
    another namespace over a connection already open, see
    :func:`client_for_profile`.
    """
    from temporalio.client import Client
    from temporalio.service import TLSConfig

    data_converter = _profile_data_converter(profile)
    tls: Any = False
    if getattr(settings, "TEMPORAL_TLS", False):
        client_cert_path = getattr(settings, "TEMPORAL_CLIENT_CERT_PATH", "")
        client_key_path = getattr(settings, "TEMPORAL_CLIENT_KEY_PATH", "")
        if client_cert_path and client_key_path:
            with open(client_cert_path, "rb") as f:
                client_cert = f.read()
            with open(client_key_path, "rb") as f:
                client_key = f.read()
            tls = TLSConfig(
                client_cert=client_cert,
                client_private_key=client_key,
            )
        else:
            tls = True

    # With the profile's key set (TEMPORAL_PAYLOAD_KEY for the default one),
    # every payload is encrypted client-side before it reaches the Temporal
    # server (see temporal_codec.py): history holds ciphertext only, and
    # destroying the key crypto-shreds whatever namespace retention has not
    # yet expired. The worker connects through this same function, so one
    # setting covers both sides.
    connect_kwargs: dict = {}
    if data_converter is not None:
        connect_kwargs["data_converter"] = data_converter
    if runtime is not None:
        connect_kwargs["runtime"] = runtime

    return await Client.connect(
        settings.TEMPORAL_HOST,
        namespace=temporal_namespace(profile),
        tls=tls,
        **connect_kwargs,
    )


def client_for_profile(client: Any, profile: str) -> Any:
    """A client in ``profile``'s namespace with that profile's payload key,
    over ``client``'s connection (its TLS and runtime too), so no new
    connection is opened. Interceptors and plugins are not carried over;
    FHI's clients have none."""
    import temporalio.converter
    from temporalio.client import Client

    return Client(
        client.service_client,
        namespace=temporal_namespace(profile),
        # Never the other client's converter: that one holds the other keys.
        data_converter=(
            _profile_data_converter(profile) or temporalio.converter.default()
        ),
    )


async def namespace_exists(client: Any) -> bool:
    """Whether the client's namespace exists on the server. A Worker in a
    namespace that does not exist fails as it starts. Raises on other RPC
    failures."""
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
    from temporalio.service import RPCError, RPCStatusCode

    try:
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=client.namespace),
            timeout=timedelta(seconds=SCHEDULE_RPC_TIMEOUT_SECONDS),
        )
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


def _encrypting_data_converter(payload_key: str, setting: str = "TEMPORAL_PAYLOAD_KEY"):
    """Payload codec + ENCODED failure attributes. The codec alone is not
    enough: Temporal's default failure converter leaves exception messages
    and stack traces as plaintext protobuf fields, so an activity error
    could leak text into history around the encryption (external review).
    With encoded attributes, message/stack move into the payload the codec
    encrypts."""
    import dataclasses as _dc

    import temporalio.converter
    from temporalio.converter import DefaultFailureConverterWithEncodedAttributes

    from fighthealthinsurance.temporal_codec import EncryptionCodec

    return _dc.replace(
        temporalio.converter.default(),
        payload_codec=EncryptionCodec(payload_key, setting),
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )


async def start_send_fax_workflow(
    hashed_email: str,
    fax_uuid: str,
    delay_send: bool = False,
    force_restart: bool = False,
) -> str:
    """Start ``SendFaxWorkflow`` for a fax. Returns the workflow id.

    ``force_restart`` (used by an explicit resend) supersedes any run already
    open for this fax. The deterministic id would otherwise raise
    ``WorkflowAlreadyStartedError``, and if that open run is already past its
    send step it never picks up a corrected destination -- terminating it and
    starting fresh re-hydrates the new destination from the DB and sends it.
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from fighthealthinsurance.workflows.types import SendFaxInput

    client = await get_temporal_client()
    conflict_policy = (
        WorkflowIDConflictPolicy.TERMINATE_EXISTING
        if force_restart
        else WorkflowIDConflictPolicy.UNSPECIFIED
    )
    handle = await client.start_workflow(
        "SendFaxWorkflow",
        SendFaxInput(
            hashed_email=hashed_email,
            fax_uuid=str(fax_uuid),
            delay_send=delay_send,
        ),
        # Deterministic id -> at most one in-flight send per fax. A resend after
        # the previous run has closed starts a fresh run (default id-reuse
        # policy); an in-flight run is superseded only when force_restart is set.
        id=f"send-fax-{fax_uuid}",
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=conflict_policy,
    )
    logger.info(f"Started SendFaxWorkflow {handle.id} for fax {fax_uuid}")
    return str(handle.id)


async def execute_send_fax_workflow(
    hashed_email: str, fax_uuid: str, delay_send: bool = False
) -> bool:
    """Start ``SendFaxWorkflow`` and wait for it to finish; returns the result.

    If a workflow for this fax is already open, attach to it and wait for its
    result instead of failing -- the activities hydrate all fax state from the
    DB at send time, so the in-flight run acts on the freshest data.

    Raises only when the workflow was definitely NOT started (connection /
    start errors), so the caller can safely fall back to Ray. Once Temporal has
    accepted the workflow this returns a bool and never signals fallback -- a
    failed or unobservable run must not trigger a second, concurrent send.
    """
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from fighthealthinsurance.workflows.types import SendFaxInput

    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            "SendFaxWorkflow",
            SendFaxInput(
                hashed_email=hashed_email,
                fax_uuid=str(fax_uuid),
                delay_send=delay_send,
            ),
            id=f"send-fax-{fax_uuid}",
            task_queue=settings.TEMPORAL_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        logger.info(f"SendFaxWorkflow already running for fax {fax_uuid}; waiting")
        handle = client.get_workflow_handle(f"send-fax-{fax_uuid}")
    # Past this point Temporal owns the send; never map errors to "fall back".
    try:
        # Bound the wait: a synchronous caller (staff blocking drain on a web
        # request) that attaches to a sleeping delay_send run must not block for
        # the whole 1-hour timer. On timeout the run keeps executing
        # server-side; we do NOT fall back (a Ray send would race it).
        return bool(await asyncio.wait_for(handle.result(), _RESULT_WAIT_SECONDS))
    except asyncio.TimeoutError:
        logger.warning(
            f"Timed out waiting for SendFaxWorkflow result for fax {fax_uuid}; "
            "it continues running server-side"
        )
        return False
    except WorkflowFailureError:
        # The workflow ran and failed; it already recorded/notified the
        # failure itself. Falling back would re-send.
        logger.opt(exception=True).error(f"SendFaxWorkflow failed for fax {fax_uuid}")
        return False
    except Exception:
        # Accepted but the result is unobservable (e.g. RPC drop mid-wait).
        # The run is still executing server-side; a Ray send would race it.
        logger.opt(exception=True).error(
            f"Lost result of SendFaxWorkflow for fax {fax_uuid}; not falling back"
        )
        return False


def dispatch_fax_send_blocking(hashed_email: str, fax_uuid: str) -> Optional[bool]:
    """Run a fax send via Temporal and block until it finishes.

    Returns the send result (True/False) when handled by Temporal, or None only
    when Temporal is disabled or the workflow was never started -- the two
    cases where the caller may safely fall back to the blocking Ray path.
    """
    if not getattr(settings, "TEMPORAL_ENABLED", False):
        return None
    try:
        return async_to_sync(execute_send_fax_workflow)(hashed_email, str(fax_uuid))
    except Exception:
        logger.opt(exception=True).error(
            "Failed to execute SendFaxWorkflow; falling back to Ray"
        )
        return None


async def start_generate_appeal_workflow(hashed_email: str, denial_uuid: str) -> str:
    """Start ``GenerateAppealWorkflow`` for a denial. Returns the workflow id.

    The deterministic id means at most one journey is in flight per denial; a
    duplicate dispatch while one is open raises ``WorkflowAlreadyStartedError``
    (handled in :func:`dispatch_appeal_generation`), and a re-dispatch after it
    closed starts a fresh run, which the precheck ends immediately when the
    drafts are already stored.
    """
    from fighthealthinsurance.workflows.types import GenerateAppealInput

    client = await get_temporal_client()
    handle = await client.start_workflow(
        "GenerateAppealWorkflow",
        GenerateAppealInput(hashed_email=hashed_email, denial_uuid=str(denial_uuid)),
        id=f"generate-appeal-{denial_uuid}",
        task_queue=settings.TEMPORAL_APPEAL_TASK_QUEUE,
    )
    logger.info(f"Started GenerateAppealWorkflow {handle.id} for denial {denial_uuid}")
    return str(handle.id)


def assistant_appeal_workflow_id(denial_uuid: str) -> str:
    return f"assistant-appeal-{denial_uuid}"


async def start_assistant_appeal_workflow(
    hashed_email: str, denial_uuid: str, client: Any = None
) -> str:
    """Start ``AssistantAppealWorkflow`` once the person has agreed. Raises
    WorkflowAlreadyStartedError when one is already open for the denial."""
    from fighthealthinsurance.workflows.types import AssistantAppealInput

    if client is None:
        client = await get_temporal_client()
    handle = await client.start_workflow(
        "AssistantAppealWorkflow",
        AssistantAppealInput(hashed_email=hashed_email, denial_uuid=str(denial_uuid)),
        id=assistant_appeal_workflow_id(str(denial_uuid)),
        task_queue=settings.TEMPORAL_APPEAL_TASK_QUEUE,
        rpc_timeout=timedelta(seconds=INTAKE_RPC_TIMEOUT_SECONDS),
    )
    return str(handle.id)


async def signal_assistant_answers_filed(denial_uuid: str, client: Any = None) -> None:
    """Tell a waiting AssistantAppealWorkflow its answers are on the denial.
    The signal carries nothing: the answers stay in Django."""
    if client is None:
        client = await get_temporal_client()
    await client.get_workflow_handle(
        assistant_appeal_workflow_id(str(denial_uuid))
    ).signal("answers_filed", rpc_timeout=timedelta(seconds=INTAKE_RPC_TIMEOUT_SECONDS))


def dispatch_appeal_generation(hashed_email: str, denial_uuid: str) -> bool:
    """Dispatch a durable appeal-generation journey when enabled.

    Returns True if the journey was handed to Temporal (or one is already in
    flight for this denial), False when Temporal or the journey flag is off or
    the start failed. There is no fallback path: this is a new queued flow, so
    a False simply means nothing was queued.
    """
    if not getattr(settings, "TEMPORAL_ENABLED", False):
        return False
    if not getattr(settings, "TEMPORAL_APPEAL_JOURNEY_ENABLED", False):
        return False
    from temporalio.exceptions import WorkflowAlreadyStartedError

    try:
        async_to_sync(start_generate_appeal_workflow)(hashed_email, str(denial_uuid))
        return True
    except WorkflowAlreadyStartedError:
        # A journey for this denial is already open; the dispatch is satisfied.
        logger.info(f"GenerateAppealWorkflow already running for denial {denial_uuid}")
        return True
    except Exception:
        logger.opt(exception=True).error(
            f"Failed to start GenerateAppealWorkflow for denial {denial_uuid}"
        )
        return False


def dispatch_fax_send(
    hashed_email: str,
    fax_uuid: str,
    delay_send: bool = False,
    force_restart: bool = False,
) -> bool:
    """Dispatch a fax send via Temporal when enabled.

    Returns True if the send was handed to Temporal, False if it was not (either
    because Temporal is disabled or because starting the workflow failed) -- in
    which case the caller should fall back to the Ray path.

    ``force_restart`` supersedes any in-flight run for this fax (see
    :func:`start_send_fax_workflow`); used by an explicit resend so a corrected
    destination is not dropped when a run is already open.
    """
    if not getattr(settings, "TEMPORAL_ENABLED", False):
        return False
    from temporalio.exceptions import WorkflowAlreadyStartedError

    try:
        async_to_sync(start_send_fax_workflow)(
            hashed_email, str(fax_uuid), delay_send, force_restart
        )
        return True
    except WorkflowAlreadyStartedError:
        # A workflow for this fax is already open -- Temporal owns it, so this
        # dispatch is satisfied. Do NOT report failure here: the caller would
        # fall back to a Ray send running concurrently with the open workflow
        # (double-fax risk). Activities hydrate fax state (e.g. a corrected
        # destination) from the DB at send time, so the open run stays correct.
        logger.info(f"SendFaxWorkflow already running for fax {fax_uuid}")
        return True
    except Exception:
        logger.opt(exception=True).error(
            "Failed to start SendFaxWorkflow; falling back to Ray"
        )
        return False


async def signal_with_start_intake(
    hashed_email: str,
    denial_uuid: str,
    contact_opt_in: bool,
    event: str,
    client: Any = None,
) -> None:
    """Start-or-signal the intake journey for a denial, atomically.

    Signal-With-Start (Temporal's own primitive for "signal it if it exists,
    start it with the signal if not") is the request-path half of the intake
    outbox (see intake_outbox.py); the Denial row's intent/ack timestamps are
    the durable half. Deterministic id: one journey per denial.

    - ``intake_started``: a plain start; an already-open journey means the
      event is already satisfied.
    - ``form_completed``: signal-with-start, so a journey whose start was
      never delivered is created already-completed instead of the signal
      vanishing.

    Returns quietly when Temporal reports the event as already satisfied.
    Raises on transport failure so the caller can leave the row pending for
    the sweep.
    """
    from datetime import timedelta

    from temporalio.client import WorkflowExecutionStatus
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from fighthealthinsurance.workflows.types import IntakeJourneyInput

    # The relay passes ONE client for a whole batch; the request path opens
    # its own for a single delivery.
    if client is None:
        client = await get_temporal_client()
    rpc_timeout = timedelta(seconds=INTAKE_RPC_TIMEOUT_SECONDS)
    workflow_id = f"intake-{denial_uuid}"
    payload = IntakeJourneyInput(
        hashed_email=hashed_email,
        denial_uuid=str(denial_uuid),
        contact_opt_in=contact_opt_in,
    )
    if event == "form_completed":
        # A RUNNING journey just receives the signal. A CLOSED one (abandoned,
        # failed, terminated) must not swallow the completion: ALLOW_DUPLICATE
        # starts a fresh run with the signal already delivered, which owns
        # generation through its child immediately (external review). An
        # "already started" here is therefore never an ack -- it propagates.
        handle = await client.start_workflow(
            "IntakeJourneyWorkflow",
            payload,
            id=workflow_id,
            task_queue=settings.TEMPORAL_APPEAL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            # The two policies cover different states and BOTH are needed:
            # reuse governs a CLOSED execution (start a fresh run), conflict
            # governs a RUNNING one (deliver into it). Signal-with-start
            # already signals a running execution without a conflict policy
            # -- verified against a real server -- so this is belt and
            # braces: it states the intent and survives a future default.
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="form_completed",
            start_signal_args=[],
            rpc_timeout=rpc_timeout,
        )
        logger.info(f"Intake event {event} delivered to {handle.id}")
        return
    try:
        handle = await client.start_workflow(
            "IntakeJourneyWorkflow",
            payload,
            id=workflow_id,
            task_queue=settings.TEMPORAL_APPEAL_TASK_QUEUE,
            # One intake journey per denial: a repeat of the first step must
            # not start a fresh run (and a fresh 24h nudge timer) for a case
            # that already has one. Bounded by namespace history retention;
            # the precheck is the durable backstop.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            rpc_timeout=rpc_timeout,
        )
    except WorkflowAlreadyStartedError:
        # Rejection alone is not an ack: verify the business fact -- that an
        # execution for this id exists (any status) -- before treating the
        # start as satisfied. If describe fails, the caller leaves the event
        # pending and the relay retries (external review).
        existing = client.get_workflow_handle(workflow_id)
        description = await existing.describe(rpc_timeout=rpc_timeout)
        if description.status == WorkflowExecutionStatus.RUNNING:
            # The journey was started by an earlier attempt with whatever
            # opt-in it had then; a still-open run must learn the CURRENT
            # value (derived from the retained raw_email at delivery time)
            # before this delivery counts as acknowledged (review).
            await existing.signal(
                "contact_opt_in", contact_opt_in, rpc_timeout=rpc_timeout
            )
        logger.info(f"Intake event {event} already satisfied by {workflow_id}")
        return
    logger.info(f"Intake event {event} delivered to {handle.id}")


async def signal_intake_contact_opt_in(
    denial_uuid: str, contact_opt_in: bool, client: Any = None
) -> None:
    """Push a changed nudge opt-in to a running intake journey. Raises on
    transport failure; callers treat it as best-effort (see intake_outbox)."""
    from datetime import timedelta

    if client is None:
        client = await get_temporal_client()
    await client.get_workflow_handle(f"intake-{denial_uuid}").signal(
        "contact_opt_in",
        contact_opt_in,
        rpc_timeout=timedelta(seconds=INTAKE_RPC_TIMEOUT_SECONDS),
    )


def _intake_enabled() -> bool:
    # Effective flags are strictly nested: global && appeal && intake. The
    # intake workflow starts GenerateAppealWorkflow as a child and both are
    # registered by the appeal-enabled worker, so intake without the appeal
    # flag would enqueue onto a queue no worker serves (external review).
    return (
        getattr(settings, "TEMPORAL_ENABLED", False)
        and getattr(settings, "TEMPORAL_APPEAL_JOURNEY_ENABLED", False)
        and getattr(settings, "TEMPORAL_INTAKE_JOURNEY_ENABLED", False)
    )


# --- The chat routing policy Schedule ---------------------------------------

# One Schedule, kept by the appeal-worker process at start-up (see
# run_temporal_worker) and by `manage.py ensure_temporal_schedules`. Each run
# writes one ChatRoutingPolicy row; history holds the window, the run id and
# the row id.
CHAT_POLICY_SCHEDULE_ID = "chat-routing-policy"
# Scheduled runs get this id with the scheduled time appended by Temporal.
CHAT_POLICY_WORKFLOW_ID = "chat-routing-policy-run"
# Daily: the live spend counters (ml/spend.py) switch models off as budgets
# run out, so this run only refreshes the learned order and the hold, and is
# the backup that rewrites the row if one run is missed.
CHAT_POLICY_EVERY = timedelta(days=1)
# After a Temporal outage, only a run missed in the last day is made up, so
# an outage ends in one run at most.
CHAT_POLICY_CATCHUP_WINDOW = timedelta(days=1)
# A run is one short activity (two minutes, three attempts); anything longer
# is stuck, and the next run is due anyway.
CHAT_POLICY_RUN_TIMEOUT = timedelta(minutes=5)
SCHEDULE_RPC_TIMEOUT_SECONDS = 10.0

SCHEDULE_CREATED = "created"
SCHEDULE_UPDATED = "updated"
SCHEDULE_PAUSED = "paused"
SCHEDULE_ABSENT = "absent"


def chat_policy_schedule_enabled() -> bool:
    """Whether the chat policy Schedule should run: TEMPORAL_ENABLED and
    TEMPORAL_CHAT_POLICY_ENABLED, independent of the journey flags."""
    return bool(
        getattr(settings, "TEMPORAL_ENABLED", False)
        and getattr(settings, "TEMPORAL_CHAT_POLICY_ENABLED", False)
    )


def chat_policy_schedule() -> Any:
    """The ``chat-routing-policy`` Schedule as it should be: once a day,
    skip a run while the last one is still going, make up at most one day
    of missed runs, and start ChatRoutingPolicyWorkflow on the
    policy task queue with a five-minute execution timeout."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleState,
    )

    from fighthealthinsurance.ml.chat_policy import DEFAULT_WINDOW_MINUTES
    from fighthealthinsurance.workflows.types import ChatRoutingPolicyInput

    return Schedule(
        action=ScheduleActionStartWorkflow(
            "ChatRoutingPolicyWorkflow",
            ChatRoutingPolicyInput(window_minutes=DEFAULT_WINDOW_MINUTES),
            id=CHAT_POLICY_WORKFLOW_ID,
            task_queue=settings.TEMPORAL_CHAT_POLICY_TASK_QUEUE,
            execution_timeout=CHAT_POLICY_RUN_TIMEOUT,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=CHAT_POLICY_EVERY)]),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=CHAT_POLICY_CATCHUP_WINDOW,
        ),
        state=ScheduleState(note="Kept in step with TEMPORAL_CHAT_POLICY_ENABLED"),
    )


async def _ensure_schedule(
    client: Any,
    schedule_id: str,
    schedule: Callable[[], Any],
    enabled: bool,
    off_note: str,
    on_note: str,
) -> str:
    """Make the Schedule ``schedule_id`` match its flag. Idempotent, and
    safe for several workers to run at once. ``off_note`` and ``on_note``
    are the notes a pause and an unpause leave on it.

    * Enabled: create it; if it already exists, replace its spec, action and
      policy with ``schedule()`` and unpause it. Returns ``"created"`` or
      ``"updated"``.
    * Disabled: pause it (``"paused"``), or do nothing when there is none
      (``"absent"``). A paused Schedule starts no runs, so a flag flip plus
      a worker restart is the operator control; the staff Temporal UI is
      read-only.

    Raises on other RPC failures; the worker logs the class name and goes on
    hosting.
    """
    from temporalio.client import ScheduleAlreadyRunningError, ScheduleUpdate
    from temporalio.service import RPCError, RPCStatusCode

    rpc_timeout = timedelta(seconds=SCHEDULE_RPC_TIMEOUT_SECONDS)
    handle = client.get_schedule_handle(schedule_id)
    if not enabled:
        try:
            await handle.pause(note=off_note, rpc_timeout=rpc_timeout)
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return SCHEDULE_ABSENT
            raise
        logger.info(f"Paused the {schedule_id} schedule")
        return SCHEDULE_PAUSED

    try:
        await client.create_schedule(schedule_id, schedule(), rpc_timeout=rpc_timeout)
        logger.info(f"Created the {schedule_id} schedule")
        return SCHEDULE_CREATED
    except ScheduleAlreadyRunningError:
        pass

    was_paused = False

    def _replace(update_input: Any) -> Any:
        nonlocal was_paused
        was_paused = bool(update_input.description.schedule.state.paused)
        return ScheduleUpdate(schedule=schedule())

    await handle.update(_replace, rpc_timeout=rpc_timeout)
    if was_paused:
        await handle.unpause(note=on_note, rpc_timeout=rpc_timeout)
    logger.info(f"Updated the {schedule_id} schedule")
    return SCHEDULE_UPDATED


async def ensure_chat_policy_schedule(client: Any, enabled: bool) -> str:
    """Make the ``chat-routing-policy`` Schedule match
    TEMPORAL_CHAT_POLICY_ENABLED (see :func:`_ensure_schedule`)."""
    return await _ensure_schedule(
        client,
        CHAT_POLICY_SCHEDULE_ID,
        chat_policy_schedule,
        enabled,
        off_note="TEMPORAL_CHAT_POLICY_ENABLED is off",
        on_note="TEMPORAL_CHAT_POLICY_ENABLED is on",
    )


# --- Shared plan documents ----------------------------------------------------


def plan_documents_enabled() -> bool:
    """Whether shared plan documents are read on Temporal: TEMPORAL_ENABLED,
    TEMPORAL_PLAN_DOCUMENTS_ENABLED and a usable TEMPORAL_RESEARCH_PAYLOAD_KEY,
    so these runs only ever exist in history that the research key alone
    reads. Independent of the journey flags."""
    if not (
        getattr(settings, "TEMPORAL_ENABLED", False)
        and getattr(settings, "TEMPORAL_PLAN_DOCUMENTS_ENABLED", False)
    ):
        return False
    return research_payload_key_problem() is None


def research_payload_key_problem() -> Optional[str]:
    """Why the research namespace and its key can't be used, or None when
    they can. A malformed key keeps the feature off everywhere, so the Ray
    email loop goes on sending reminders. A key or namespace it shares with
    the default profile would mix the two namespaces' history."""
    from cryptography.fernet import Fernet

    from fighthealthinsurance.temporal_codec import payload_keys

    research = payload_keys(getattr(settings, "TEMPORAL_RESEARCH_PAYLOAD_KEY", ""))
    if not research:
        return "TEMPORAL_RESEARCH_PAYLOAD_KEY is unset"
    for key in research:
        try:
            Fernet(key)
        except (ValueError, TypeError):
            return "TEMPORAL_RESEARCH_PAYLOAD_KEY holds a key that is not a Fernet key"
    shared = set()
    for key in payload_keys(getattr(settings, "TEMPORAL_PAYLOAD_KEY", "")):
        try:
            shared.add(base64.urlsafe_b64decode(key))
        except (ValueError, TypeError):
            # Not a key, so it can't read research history.
            continue
    if {base64.urlsafe_b64decode(key) for key in research} & shared:
        return "TEMPORAL_RESEARCH_PAYLOAD_KEY shares a key with TEMPORAL_PAYLOAD_KEY"
    if temporal_namespace(RESEARCH_PROFILE) == temporal_namespace(DEFAULT_PROFILE):
        return "TEMPORAL_RESEARCH_NAMESPACE is the same as TEMPORAL_NAMESPACE"
    return None


# The longest a share waits on Temporal for its reading to start, connecting
# included. Past it the share is done all the same, and the sweep starts the
# reading later.
PLAN_DOCUMENTS_START_WAIT_SECONDS = 3.0


async def start_shared_plan_documents_workflow(
    share_batch: str, client: Any = None
) -> str:
    """Start ``SharedPlanDocumentsWorkflow`` for one share. Raises
    WorkflowAlreadyStartedError when one is open for it. A closed run can be
    followed by a new one, which reads only the rows still waiting."""
    from fighthealthinsurance.workflows.shared_plan_documents import (
        shared_plan_documents_workflow_id,
    )
    from fighthealthinsurance.workflows.types import SharedPlanDocumentsInput

    if client is None:
        client = await get_temporal_client(profile=RESEARCH_PROFILE)
    handle = await client.start_workflow(
        "SharedPlanDocumentsWorkflow",
        SharedPlanDocumentsInput(share_batch=str(share_batch)),
        id=shared_plan_documents_workflow_id(str(share_batch)),
        task_queue=settings.TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE,
        rpc_timeout=timedelta(seconds=PLAN_DOCUMENTS_START_WAIT_SECONDS),
    )
    return str(handle.id)


def dispatch_shared_plan_documents(share_batch: str) -> bool:
    """Start reading a share's files, best effort, from the request that
    saved them. True when Temporal has the share (a run already open for it
    counts), False when the flags are off or the start failed. Never raises,
    and waits PLAN_DOCUMENTS_START_WAIT_SECONDS at most: a lost start only
    means the hourly sweep starts it instead."""
    if not plan_documents_enabled():
        return False
    from temporalio.exceptions import WorkflowAlreadyStartedError

    async def _start() -> str:
        return await asyncio.wait_for(
            start_shared_plan_documents_workflow(str(share_batch)),
            PLAN_DOCUMENTS_START_WAIT_SECONDS,
        )

    try:
        async_to_sync(_start)()
        return True
    except WorkflowAlreadyStartedError:
        return True
    except Exception as e:
        # Class name only; the sweep is the retry.
        logger.warning(
            f"Shared plan documents not started on Temporal: {type(e).__name__}"
        )
        return False


# One Schedule, kept by the appeal-worker process at start-up and by
# `manage.py ensure_temporal_schedules`, like the chat policy's.
PLAN_DOCUMENTS_SCHEDULE_ID = "plan-documents-sweep"
PLAN_DOCUMENTS_OFF_NOTE = (
    "plan_documents_enabled() is false (it needs TEMPORAL_ENABLED, "
    "TEMPORAL_PLAN_DOCUMENTS_ENABLED and a TEMPORAL_RESEARCH_PAYLOAD_KEY "
    "of its own)"
)
# Scheduled runs get this id with the scheduled time appended by Temporal.
PLAN_DOCUMENTS_SWEEP_WORKFLOW_ID = "plan-documents-sweep-run"
# Hourly: a reminder goes within the hour it falls due, and a share whose
# start was lost waits about an hour at most.
PLAN_DOCUMENTS_SWEEP_EVERY = timedelta(hours=1)
# After a Temporal outage only a run missed in the last hour is made up, so
# an outage ends in one run at most.
PLAN_DOCUMENTS_SWEEP_CATCHUP_WINDOW = timedelta(hours=1)
# A run is a few lookups, up to fifty one-minute sends and some starts.
PLAN_DOCUMENTS_SWEEP_RUN_TIMEOUT = timedelta(minutes=30)


def plan_documents_schedule() -> Any:
    """The ``plan-documents-sweep`` Schedule as it should be: once an hour,
    skip a run while the last one is still going, make up at most one hour
    of missed runs, and start PlanDocumentsSweepWorkflow on the plan
    documents task queue with a 30-minute execution timeout."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleState,
    )

    from fighthealthinsurance.workflows.types import PlanDocumentsSweepInput

    return Schedule(
        action=ScheduleActionStartWorkflow(
            "PlanDocumentsSweepWorkflow",
            PlanDocumentsSweepInput(),
            id=PLAN_DOCUMENTS_SWEEP_WORKFLOW_ID,
            task_queue=settings.TEMPORAL_PLAN_DOCUMENTS_TASK_QUEUE,
            execution_timeout=PLAN_DOCUMENTS_SWEEP_RUN_TIMEOUT,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=PLAN_DOCUMENTS_SWEEP_EVERY)]
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=PLAN_DOCUMENTS_SWEEP_CATCHUP_WINDOW,
        ),
        state=ScheduleState(
            note="Kept in step with temporal_client.plan_documents_enabled()"
        ),
    )


async def ensure_plan_documents_schedule(client: Any, enabled: bool) -> str:
    """Make the ``plan-documents-sweep`` Schedule match
    :func:`plan_documents_enabled` (see :func:`_ensure_schedule`). ``client``
    must be in the research namespace, where the Schedule and its runs live."""
    if client.namespace != temporal_namespace(RESEARCH_PROFILE):
        raise ValueError(
            f"The {PLAN_DOCUMENTS_SCHEDULE_ID} Schedule lives in the research namespace"
        )
    return await _ensure_schedule(
        client,
        PLAN_DOCUMENTS_SCHEDULE_ID,
        plan_documents_schedule,
        enabled,
        # The flag alone is not the whole condition.
        off_note=PLAN_DOCUMENTS_OFF_NOTE,
        on_note="plan_documents_enabled() is true",
    )


# The fire-and-forget dispatch/signal helpers that used to live here were
# lossy (a swallowed failure or a process recycle lost the event with no
# trace); intake_outbox.py replaced them with intent/ack bookkeeping on the
# Denial row plus signal_with_start_intake above (external review).

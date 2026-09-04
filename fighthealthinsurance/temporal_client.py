"""Temporal client connection + fax-dispatch helpers.

All ``temporalio`` imports are done lazily inside the functions so that simply
importing this module (e.g. from ``fax_helpers``) does not require ``temporalio``
or touch the network. When ``settings.TEMPORAL_ENABLED`` is False -- the default
-- :func:`dispatch_fax_send` short-circuits before importing anything Temporal,
leaving the existing Ray path entirely untouched.
"""

import asyncio
from typing import Any, Optional

from asgiref.sync import async_to_sync
from django.conf import settings

from loguru import logger

# Upper bound on how long a *blocking* dispatch waits for a workflow result
# before returning (the run keeps executing server-side). Without this a
# synchronous caller that attaches to a sleeping ``delay_send`` run would block
# for the full 1-hour delay timer -- long enough to exhaust a web worker.
_RESULT_WAIT_SECONDS = 15 * 60


async def get_temporal_client(runtime: Any = None) -> Any:
    """Connect a Temporal client using Django settings.

    Supports a plain connection (local dev server), server-side TLS, and mTLS
    with client cert/key files for a self-hosted cluster.

    ``runtime`` is the SDK ``temporalio.runtime.Runtime`` carrying telemetry
    config; only the worker process passes one (it hosts the Prometheus
    metrics endpoint). Web/Ray callers leave it None and get the default.
    """
    from temporalio.client import Client
    from temporalio.service import TLSConfig

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

    # With TEMPORAL_PAYLOAD_KEY set, every payload is encrypted client-side
    # before it reaches the Temporal server (see temporal_codec.py): history
    # holds ciphertext only, and destroying the key crypto-shreds whatever
    # namespace retention has not yet expired. The worker connects through
    # this same function, so one setting covers both sides.
    connect_kwargs: dict = {}
    payload_key = getattr(settings, "TEMPORAL_PAYLOAD_KEY", "")
    if payload_key:
        connect_kwargs["data_converter"] = _encrypting_data_converter(payload_key)
    if runtime is not None:
        connect_kwargs["runtime"] = runtime

    return await Client.connect(
        settings.TEMPORAL_HOST,
        namespace=settings.TEMPORAL_NAMESPACE,
        tls=tls,
        **connect_kwargs,
    )


def _encrypting_data_converter(payload_key: str):
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
        payload_codec=EncryptionCodec(payload_key),
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
    hashed_email: str, denial_uuid: str, contact_opt_in: bool, event: str
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
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from fighthealthinsurance.workflows.types import IntakeJourneyInput

    client = await get_temporal_client()
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
            start_signal="form_completed",
            start_signal_args=[],
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
        )
    except WorkflowAlreadyStartedError:
        # Rejection alone is not an ack: verify the business fact -- that an
        # execution for this id exists (any status) -- before treating the
        # start as satisfied. If describe fails, the caller leaves the event
        # pending and the relay retries (external review).
        await client.get_workflow_handle(workflow_id).describe()
        logger.info(f"Intake event {event} already satisfied by {workflow_id}")
        return
    logger.info(f"Intake event {event} delivered to {handle.id}")


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


# The fire-and-forget dispatch/signal helpers that used to live here were
# lossy (a swallowed failure or a process recycle lost the event with no
# trace); intake_outbox.py replaced them with intent/ack bookkeeping on the
# Denial row plus signal_with_start_intake above (external review).

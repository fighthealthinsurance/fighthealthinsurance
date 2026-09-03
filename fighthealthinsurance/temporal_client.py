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


async def get_temporal_client() -> Any:
    """Connect a Temporal client using Django settings.

    Supports a plain connection (local dev server), server-side TLS, and mTLS
    with client cert/key files for a self-hosted cluster.
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


async def start_intake_journey(
    hashed_email: str, denial_uuid: str, contact_opt_in: bool
) -> str:
    """Start (or attach to) the intake journey for a denial.

    Deterministic id: one journey per denial; a duplicate start while one is
    open raises WorkflowAlreadyStartedError, which the dispatcher treats as
    satisfied.
    """
    from fighthealthinsurance.workflows.types import IntakeJourneyInput

    from temporalio.common import WorkflowIDReusePolicy

    client = await get_temporal_client()
    handle = await client.start_workflow(
        "IntakeJourneyWorkflow",
        IntakeJourneyInput(
            hashed_email=hashed_email,
            denial_uuid=str(denial_uuid),
            contact_opt_in=contact_opt_in,
        ),
        id=f"intake-{denial_uuid}",
        task_queue=settings.TEMPORAL_APPEAL_TASK_QUEUE,
        # One intake journey per denial: re-running the form update after a
        # completed journey must not start a fresh run (and a fresh 24h
        # nudge timer) for a finished case. NOTE this guarantee is bounded
        # by namespace history retention (720h) -- after the old run's
        # history expires, the id becomes startable again. The durable
        # backstop is the precheck: existing drafts end a journey at its
        # first step, so a post-retention duplicate no-ops (external review).
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    logger.info(f"Started IntakeJourneyWorkflow {handle.id}")
    return str(handle.id)


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


def dispatch_intake_started(
    hashed_email: str, denial_uuid: str, contact_opt_in: bool
) -> bool:
    """Fire-and-forget: the user reached the first substantive step."""
    if not _intake_enabled():
        return False
    from temporalio.exceptions import WorkflowAlreadyStartedError

    try:
        async_to_sync(start_intake_journey)(hashed_email, denial_uuid, contact_opt_in)
        return True
    except WorkflowAlreadyStartedError:
        return True
    except Exception:
        logger.opt(exception=True).error("Failed to start intake journey")
        return False


def signal_intake(denial_uuid: str, signal: str, *args) -> bool:
    """Fire-and-forget signal to an intake journey (step_reached,
    contact_opt_in, form_completed). Never raises: the journey observes the
    funnel and must not be able to break the user-facing flow."""
    if not _intake_enabled():
        return False

    async def _send() -> None:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(f"intake-{denial_uuid}")
        await handle.signal(signal, *args)

    try:
        async_to_sync(_send)()
        return True
    except Exception:
        logger.opt(exception=True).warning(
            f"Intake signal {signal} not delivered for denial {denial_uuid}"
        )
        return False


async def asignal_intake_fire_and_forget(denial_uuid: str, signal: str) -> None:
    """Async fire-and-forget intake signal for callers already on a loop
    (e.g. generate_appeals). Spawns a task and swallows every failure --
    the journey observes; it never gates the user-facing flow."""
    if not _intake_enabled():
        return
    import asyncio as _asyncio

    async def _send() -> None:
        try:
            client = await get_temporal_client()
            await client.get_workflow_handle(f"intake-{denial_uuid}").signal(signal)
        except Exception:
            logger.opt(exception=True).warning(
                f"Intake signal {signal} not delivered for denial {denial_uuid}"
            )

    _asyncio.create_task(_send())

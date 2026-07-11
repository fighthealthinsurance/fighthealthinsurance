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

    return await Client.connect(
        settings.TEMPORAL_HOST,
        namespace=settings.TEMPORAL_NAMESPACE,
        tls=tls,
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

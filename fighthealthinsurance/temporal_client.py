"""Temporal client connection + fax-dispatch helpers.

All ``temporalio`` imports are done lazily inside the functions so that simply
importing this module (e.g. from ``fax_helpers``) does not require ``temporalio``
or touch the network. When ``settings.TEMPORAL_ENABLED`` is False -- the default
-- :func:`dispatch_fax_send` short-circuits before importing anything Temporal,
leaving the existing Ray path entirely untouched.
"""

from typing import Any, Optional

from asgiref.sync import async_to_sync
from django.conf import settings

from loguru import logger


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
    hashed_email: str, fax_uuid: str, delay_send: bool = False
) -> str:
    """Start ``SendFaxWorkflow`` for a fax. Returns the workflow id."""
    from fighthealthinsurance.workflows.types import SendFaxInput

    client = await get_temporal_client()
    handle = await client.start_workflow(
        "SendFaxWorkflow",
        SendFaxInput(
            hashed_email=hashed_email,
            fax_uuid=str(fax_uuid),
            delay_send=delay_send,
        ),
        # Deterministic id -> at most one in-flight send per fax. A resend after
        # the previous run has closed starts a fresh run (default id-reuse
        # policy), which is exactly what we want.
        id=f"send-fax-{fax_uuid}",
        task_queue=settings.TEMPORAL_TASK_QUEUE,
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
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from fighthealthinsurance.workflows.types import SendFaxInput

    client = await get_temporal_client()
    try:
        return bool(
            await client.execute_workflow(
                "SendFaxWorkflow",
                SendFaxInput(
                    hashed_email=hashed_email,
                    fax_uuid=str(fax_uuid),
                    delay_send=delay_send,
                ),
                id=f"send-fax-{fax_uuid}",
                task_queue=settings.TEMPORAL_TASK_QUEUE,
            )
        )
    except WorkflowAlreadyStartedError:
        logger.info(f"SendFaxWorkflow already running for fax {fax_uuid}; waiting")
        handle = client.get_workflow_handle(f"send-fax-{fax_uuid}")
        return bool(await handle.result())


def dispatch_fax_send_blocking(hashed_email: str, fax_uuid: str) -> Optional[bool]:
    """Run a fax send via Temporal and block until it finishes.

    Returns the send result (True/False) when handled by Temporal, or None when
    Temporal is disabled or the dispatch failed -- in which case the caller
    should fall back to the blocking Ray path.
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
    hashed_email: str, fax_uuid: str, delay_send: bool = False
) -> bool:
    """Dispatch a fax send via Temporal when enabled.

    Returns True if the send was handed to Temporal, False if it was not (either
    because Temporal is disabled or because starting the workflow failed) -- in
    which case the caller should fall back to the Ray path.
    """
    if not getattr(settings, "TEMPORAL_ENABLED", False):
        return False
    from temporalio.exceptions import WorkflowAlreadyStartedError

    try:
        async_to_sync(start_send_fax_workflow)(hashed_email, str(fax_uuid), delay_send)
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

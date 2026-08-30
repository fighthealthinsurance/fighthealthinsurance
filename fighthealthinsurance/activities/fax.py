"""Temporal activities for sending appeal faxes.

Thin, **synchronous** wrappers around :mod:`fighthealthinsurance.fax_send_core`.
They are sync (not async) because they do blocking Django ORM queries and a
blocking vendor fax call; the worker runs them in a ``ThreadPoolExecutor`` so
they never block the async event loop. Each activity re-loads the fax from its
opaque ``(hashed_email, uuid)`` identifiers, so no PHI is passed in/out or
stored in workflow history.

``close_old_connections`` is called at the top of each activity because
activities run on pooled worker threads and Django connections are
thread-local; this drops any connection left stale/closed by a prior task.
"""

import threading

from django.db import close_old_connections

from loguru import logger

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import fax_send_core
from fighthealthinsurance.fax_status import STATUS_NOT_FOUND


@activity.defn
def precheck_fax(hashed_email: str, fax_uuid: str) -> str:
    """Validate the fax and mark it attempting; returns a ``STATUS_*`` string."""
    close_old_connections()
    fax = fax_send_core.load_fax(hashed_email, fax_uuid)
    if fax is None:
        return STATUS_NOT_FOUND
    return fax_send_core.precheck_fax(fax)


# How often the send activity heartbeats while the vendor call runs. The
# workflow pairs this with ``heartbeat_timeout`` so a worker killed mid-send
# (OOM, eviction) is detected in ~1-2 minutes instead of sitting silent until
# the 30-minute start-to-close timeout (observed 2026-08-30).
HEARTBEAT_INTERVAL_S = 10.0


def _call_with_heartbeats(fn, *args):
    """Run blocking ``fn`` in an inner thread, heartbeating while it works.

    The heartbeats are a liveness signal only (no progress details). If this
    worker process dies, the heartbeats stop and the server fails the attempt
    with a HEARTBEAT timeout -- which the workflow treats as "the thread is
    gone", the one failure mode where an automatic re-send cannot double-fax.
    """
    box: dict = {}

    def _target():
        try:
            box["result"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - re-raised on the activity thread
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    while t.is_alive():
        t.join(timeout=HEARTBEAT_INTERVAL_S)
        try:
            activity.heartbeat()
        except RuntimeError:
            # Not inside an activity (direct call in tests): just wait it out.
            t.join()
            break
    if "error" in box:
        raise box["error"]
    return box.get("result")


@activity.defn
def send_fax_via_vendor(hashed_email: str, fax_uuid: str) -> bool:
    """Send the fax through the vendor; returns whether it succeeded."""
    close_old_connections()
    fax = fax_send_core.load_fax(hashed_email, fax_uuid)
    if fax is None:
        return False
    try:
        return bool(_call_with_heartbeats(fax_send_core.send_fax_via_vendor, fax))
    except Exception:
        # Temporal records raised exception messages + tracebacks verbatim in
        # durable workflow history. Keep the full detail in the worker logs,
        # but raise a sanitized (still retryable) error carrying only the
        # opaque uuid so document/vendor exception text can never leak
        # sensitive strings into history.
        logger.opt(exception=True).error(f"Vendor send failed for fax {fax_uuid}")
        raise ApplicationError(f"vendor send failed for fax {fax_uuid}") from None


@activity.defn
def release_send_claim(hashed_email: str, fax_uuid: str) -> bool:
    """Release the vendor-send claim after a failed attempt so resends work.

    Idempotent; returns whether the fax was found. Run by the workflow after
    any failed send, because an in-process release cannot happen when the
    worker died holding the claim.
    """
    close_old_connections()
    fax = fax_send_core.load_fax(hashed_email, fax_uuid)
    if fax is None:
        return False
    fax_send_core.release_send_claim(fax)
    return True


@activity.defn
def finalize_fax(
    hashed_email: str, fax_uuid: str, fax_success: bool, missing_destination: bool
) -> bool:
    """Record the send outcome and send the user follow-up / update the appeal."""
    close_old_connections()
    fax = fax_send_core.load_fax(hashed_email, fax_uuid)
    if fax is None:
        return False
    return fax_send_core.finalize_fax(fax, fax_success, missing_destination)

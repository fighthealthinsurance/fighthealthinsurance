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

from django.db import close_old_connections

from temporalio import activity

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


@activity.defn
def send_fax_via_vendor(hashed_email: str, fax_uuid: str) -> bool:
    """Send the fax through the vendor; returns whether it succeeded."""
    close_old_connections()
    fax = fax_send_core.load_fax(hashed_email, fax_uuid)
    if fax is None:
        return False
    return fax_send_core.send_fax_via_vendor(fax)


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

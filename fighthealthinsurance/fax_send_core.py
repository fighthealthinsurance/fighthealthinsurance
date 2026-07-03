"""Shared, orchestrator-agnostic fax-send steps.

These functions hold the actual fax-sending business logic so that the Ray
``FaxActor`` and the Temporal fax activities run *identical* code rather than
two drifting copies. Each function operates on a loaded ``FaxesToSend``
instance; callers that only hold the opaque ``(hashed_email, uuid)`` identifiers
use :func:`load_fax` first.

Only the hashed email + uuid ever need to cross an orchestration boundary, so no
PHI is handed to Ray or written into Temporal workflow history -- the document,
destination, and patient details are re-hydrated from the database inside each
step.
"""

import asyncio
import os
from typing import TYPE_CHECKING, Optional, Union
from uuid import UUID

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from loguru import logger

from fighthealthinsurance.fax_status import (
    STATUS_ALREADY_SENT,
    STATUS_MISSING_DENIAL,
    STATUS_MISSING_DESTINATION,
    STATUS_NOT_FOUND,
    STATUS_OK,
)
from fighthealthinsurance.fax_utils import flexible_fax_magic
from fighthealthinsurance.utils import get_env_variable

if TYPE_CHECKING:
    from fighthealthinsurance.models import FaxesToSend


def send_fax_status_notification(
    fax, fax_success, missing_destination, missing_denial=False
):
    """Send internal notification email about fax status to support."""
    notify = get_env_variable("FAX_STATUS_NOTIFICATIONS", "true").lower() == "true"
    if not notify:
        return
    status = "SUCCESS" if fax_success else "FAILED"
    if missing_destination:
        status = "FAILED (missing destination)"
    if missing_denial:
        status = "FAILED (missing denial)"
    denial_id = getattr(fax.denial_id, "pk", None)
    body = (
        f"Fax Status: {status}\n"
        f"Fax ID: {fax.fax_id}\n"
        f"UUID: {fax.uuid}\n"
        f"Destination: {fax.destination or 'N/A'}\n"
        f"Denial ID: {denial_id}\n"
        f"Professional: {fax.professional}\n"
    )
    try:
        send_mail(
            f"Fax {status} - ID {fax.fax_id}",
            body,
            settings.DEFAULT_FROM_EMAIL,
            ["support42@fighthealthinsurance.com"],
        )
    except Exception:
        logger.opt(exception=True).error("Error sending fax status notification")


def load_fax(hashed_email: str, fax_uuid: Union[str, UUID]) -> Optional["FaxesToSend"]:
    """Look up a fax by its opaque ``(hashed_email, uuid)`` identifiers."""
    from fighthealthinsurance.models import FaxesToSend

    if not isinstance(fax_uuid, str):
        fax_uuid = str(fax_uuid)
    try:
        return FaxesToSend.objects.filter(
            uuid=fax_uuid, hashed_email=hashed_email
        ).get()
    except FaxesToSend.DoesNotExist:
        logger.warning(f"Fax not found for uuid={fax_uuid}")
        return None


def precheck_fax(fax: "FaxesToSend") -> str:
    """Validate a fax and, when ready, mark it as being attempted.

    Returns one of the ``STATUS_*`` constants. ``STATUS_OK`` means the fax has a
    denial and destination and has been stamped ``attempting_to_send_as_of`` --
    the caller should proceed to :func:`send_fax_via_vendor`.
    """
    if fax.sent and fax.fax_success:
        # Idempotency guard: never re-send a fax that already went out
        # successfully (e.g. a duplicate Temporal dispatch or resend race).
        # A *failed* attempt (sent=True, fax_success=False) may be retried,
        # matching the original FaxActor behavior.
        logger.info(f"Fax uuid={fax.uuid} already sent successfully; skipping")
        return STATUS_ALREADY_SENT
    denial = fax.denial_id
    if denial is None:
        logger.warning(f"Fax {fax} has no denial id")
        send_fax_status_notification(fax, False, False, missing_denial=True)
        return STATUS_MISSING_DENIAL
    if fax.destination is None:
        logger.warning(f"Fax {fax} has no destination")
        return STATUS_MISSING_DESTINATION
    fax.attempting_to_send_as_of = timezone.now()
    fax.save()
    return STATUS_OK


def send_fax_via_vendor(fax: "FaxesToSend") -> bool:
    """Send the fax document through the fax vendor. Returns success.

    Idempotent on success: once a send has been handed to the vendor we record
    ``vendor_send_completed`` and short-circuit any later call, so a retry (e.g.
    Temporal re-running the activity after a worker crash) never double-faxes.

    Transport errors are *not* swallowed here -- they propagate so a caller with
    a retry policy (the Temporal workflow) can retry a genuinely failed send. The
    Ray orchestrator in :func:`do_send_fax_object` catches them instead, matching
    the prior actor behavior.
    """
    if fax.vendor_send_completed:
        logger.info(f"Fax uuid={fax.uuid} already handed to vendor; not re-sending")
        return True
    destination = fax.destination
    if destination is None:
        # precheck_fax already screens this out; guard here too so a direct
        # caller can't crash the vendor send on a missing fax number.
        logger.warning(f"Fax uuid={fax.uuid} has no destination at send time")
        return False
    denial = fax.denial_id
    extra = ""
    if denial is not None and denial.claim_id is not None and len(denial.claim_id) > 2:
        extra += f"This is regarding claim id {denial.claim_id}."
    if fax.name is not None and len(fax.name) > 2:
        extra += f"This fax is sent on behalf of {fax.name}."
    logger.debug(f"Kicking off fax sending for uuid={fax.uuid}")
    # get_temporary_document_path leaves cleanup to the caller (delete=False),
    # so remove the temp file on both success and failure paths.
    document_path = fax.get_temporary_document_path()
    try:
        result = asyncio.run(
            flexible_fax_magic.send_fax(
                input_paths=[document_path],
                extra=extra,
                destination=destination,
                blocking=True,
                professional=fax.professional,
            )
        )
    finally:
        try:
            os.unlink(document_path)
        except OSError:
            pass
    if result:
        try:
            fax.vendor_send_completed = True
            fax.save(update_fields=["vendor_send_completed"])
        except Exception:
            # The vendor already accepted the fax. Raising here would make the
            # Temporal retry policy re-send an already-delivered document, so
            # log and report success; finalize (which retries indefinitely)
            # records the outcome once the DB recovers.
            logger.opt(exception=True).error(
                f"Failed to persist vendor_send_completed for fax uuid={fax.uuid}"
            )
    return result


def finalize_fax(
    fax: "FaxesToSend", fax_success: bool, missing_destination: bool
) -> bool:
    """Record the send outcome and notify the user.

    Marks the fax sent (the durable, must-happen step), then best-effort sends
    the support notification and the user follow-up email / updates the appeal.
    Notification/follow-up failures are logged rather than raised so a flaky
    email never undoes an already-sent fax or triggers a re-send on retry.
    """
    email = fax.email
    fax.sent = True
    fax.fax_success = fax_success
    fax.save()
    send_fax_status_notification(fax, fax_success, missing_destination)
    logger.debug(
        f"Fax uuid={fax.uuid} sent (success={fax_success}); checking user notification"
    )
    if fax.professional:
        appeal = fax.for_appeal
        if appeal is not None:
            appeal.sent = fax_success
            appeal.save()
            return True
        else:
            logger.warning(f"No appeal found for professional {fax}")
            return True
    from fighthealthinsurance.email_utils import is_blocked_email

    if is_blocked_email(email):
        logger.info("Skipping fax follow-up email to blocked address")
        return True
    try:
        fax_redo_link = "https://www.fighthealthinsurance.com" + reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": fax.uuid,
            },
        )
        context = {
            "name": fax.name,
            "success": fax_success,
            "fax_redo_link": fax_redo_link,
            "missing_destination": missing_destination,
        }
        # First, render the plain text content.
        text_content = render_to_string(
            "emails/fax_followup.txt",
            context=context,
        )

        # Secondly, render the HTML content.
        html_content = render_to_string(
            "emails/fax_followup.html",
            context=context,
        )
        # Then, create a multipart email instance.
        msg = EmailMultiAlternatives(
            "Following up from Fight Health Insurance Fax Service",
            text_content,
            "support42@fighthealthinsurance.com",
            [email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        logger.info("Fax follow-up email sent")
    except Exception:
        logger.opt(exception=True).error("Error sending fax follow-up email")
    return True


def do_send_fax_object(fax: "FaxesToSend") -> bool:
    """Run the full precheck -> send -> finalize sequence for a loaded fax.

    Shared by the Ray ``FaxActor`` and exercised step-by-step by the Temporal
    ``SendFaxWorkflow`` (which runs each step as its own activity).
    """
    status = precheck_fax(fax)
    if status == STATUS_MISSING_DENIAL:
        return False
    if status == STATUS_MISSING_DESTINATION:
        finalize_fax(fax, False, missing_destination=True)
        return False
    if status in (STATUS_ALREADY_SENT, STATUS_NOT_FOUND):
        return False
    try:
        success = send_fax_via_vendor(fax)
    except Exception:
        # The Temporal workflow retries the send activity via its retry policy;
        # the Ray path records a failed send and notifies, as it did before.
        logger.opt(exception=True).error(f"Error sending fax uuid={fax.uuid}")
        success = False
    finalize_fax(fax, success, missing_destination=False)
    return success

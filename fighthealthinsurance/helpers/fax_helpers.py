"""
Fax sending helpers for Fight Health Insurance.

Provides utilities for staging and sending appeal faxes.
"""

from dataclasses import dataclass
from typing import Optional

import ray

from loguru import logger

from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.models import Appeal, Denial, FaxesToSend
from fighthealthinsurance.temporal_client import (
    dispatch_fax_send,
    dispatch_fax_send_blocking,
)


def _dispatch_or_ray_fax(
    hashed_email: str, fax_uuid: str, force_restart: bool = False
) -> None:
    """Send a fax via Temporal when enabled, otherwise via the Ray fax actor.

    Keeps a single switch point: when ``TEMPORAL_ENABLED`` is set the send runs
    as a durable ``SendFaxWorkflow``; otherwise (or if dispatch fails) it falls
    back to the existing non-blocking Ray ``do_send_fax`` call. ``force_restart``
    (an explicit resend) supersedes any in-flight workflow for this fax.
    """
    if not dispatch_fax_send(hashed_email, str(fax_uuid), force_restart=force_restart):
        fax_actor_ref.get.do_send_fax.remote(hashed_email, str(fax_uuid))


def _blocking_dispatch_or_ray_fax(hashed_email: str, fax_uuid: str) -> None:
    """Send a fax and block until it finishes, via Temporal when enabled else Ray."""
    if dispatch_fax_send_blocking(hashed_email, str(fax_uuid)) is None:
        ray.get(fax_actor_ref.get.do_send_fax.remote(hashed_email, str(fax_uuid)))


@dataclass
class FaxHelperResults:
    """Results from staging a fax for sending."""

    uuid: str
    hashed_email: str


class SendFaxHelper:
    """Helper class for sending appeal faxes."""

    @classmethod
    def stage_appeal_as_fax(
        cls,
        appeal: Appeal,
        email: str,
        professional: bool = False,
        fax_number: Optional[str] = None,
    ) -> FaxHelperResults:
        """
        Stage an appeal to be sent as a fax.

        Args:
            appeal: The appeal to send
            email: Email address associated with the appeal
            professional: Whether this is from a professional user
            fax_number: Optional override fax number

        Returns:
            FaxHelperResults with uuid and hashed_email

        Raises:
            Exception: If appeal has no denial or no appeal text
        """
        denial = appeal.for_denial
        if denial is None:
            raise Exception("No denial")
        appeal_fax_number = denial.appeal_fax_number
        hashed_email = Denial.get_hashed_email(email)
        appeal_text = appeal.appeal_text
        if not appeal_text:
            raise Exception("No appeal text")
        fts = FaxesToSend.objects.create(
            paid=True,
            pmids=appeal.pubmed_ids_json,
            hashed_email=hashed_email,
            appeal_text=appeal_text,
            email=email,
            denial_id=denial,
            combined_document_enc=appeal.document_enc,
            destination=appeal_fax_number or fax_number,
            professional=professional,
        )
        appeal.fax = fts
        appeal.save()
        # We call str on fts.uuid since it's a UUID object but when persisted it's a string
        _dispatch_or_ray_fax(fts.hashed_email, str(fts.uuid))
        return FaxHelperResults(uuid=str(fts.uuid), hashed_email=hashed_email)

    @classmethod
    def blocking_dosend_target(cls, email: str) -> int:
        """
        Send all pending faxes for a specific email, blocking until complete.

        Args:
            email: Email address to send faxes for

        Returns:
            Number of faxes sent
        """
        faxes = FaxesToSend.objects.filter(email=email, sent=False)
        c = 0
        for f in faxes:
            _blocking_dispatch_or_ray_fax(f.hashed_email, str(f.uuid))
            c = c + 1
        return c

    @classmethod
    def blocking_dosend_all(cls, count: int) -> int:
        """
        Send a batch of pending faxes, blocking until complete.

        Args:
            count: Maximum number of faxes to send

        Returns:
            Number of faxes sent
        """
        faxes = FaxesToSend.objects.filter(sent=False)[0:count]
        c = 0
        for fax in faxes:
            _blocking_dispatch_or_ray_fax(fax.hashed_email, str(fax.uuid))
            c = c + 1
        return c

    @classmethod
    def resend(cls, fax_phone: str, uuid: str, hashed_email: str) -> bool:
        """
        Resend a fax to a new phone number.

        Args:
            fax_phone: New destination fax number
            uuid: UUID of the fax to resend
            hashed_email: Hashed email for lookup

        Returns:
            True if resend was initiated
        """
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.destination = fax_phone
        f.should_send = True
        f.sent = (
            False  # Technically not necessary, but set in case the live actor fails
        )
        # An explicit resend must actually re-transmit: clear the vendor
        # idempotency marker or send_fax_via_vendor would short-circuit and
        # report success without faxing the new number.
        f.vendor_send_completed = False
        f.save()
        # force_restart supersedes any workflow still open for this fax so the
        # corrected destination is not dropped by a run already past its send
        # step (which would never re-read the new number).
        _dispatch_or_ray_fax(hashed_email, uuid, force_restart=True)
        return True

    @classmethod
    def remote_send_fax(cls, hashed_email: str, uuid: str) -> str:
        """
        Send a fax using ray non-blocking.

        Args:
            hashed_email: Hashed email for the fax
            uuid: UUID of the fax to send

        Returns:
            ``"already_sent"`` if the fax already went out successfully (so the
            caller can confirm receipt instead of silently re-rendering a
            "sending" page), otherwise ``"dispatched"``.
        """
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        if f.sent and f.fax_success:
            # Idempotent request -- e.g. a browser refresh/prefetch of the Stripe
            # success page re-hitting this GET. The fax already went out; don't
            # re-dispatch, and tell the caller so it can show a real confirmation
            # rather than an identical "thank you, sending" page.
            logger.info(
                f"Fax uuid={uuid} already sent successfully; confirming receipt"
            )
            return "already_sent"
        # Mark fax as to be sent just in case ray doesn't follow through
        f.should_send = True
        f.paid = True
        f.save()
        _dispatch_or_ray_fax(hashed_email, uuid)
        return "dispatched"

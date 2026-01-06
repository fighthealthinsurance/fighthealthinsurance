"""
Fax sending helpers for Fight Health Insurance.

Provides utilities for staging and sending appeal faxes.
"""

from dataclasses import dataclass
from typing import Optional

import ray

from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.models import Appeal, Denial, FaxesToSend


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
        fax_actor_ref.get.do_send_fax.remote(fts.hashed_email, str(fts.uuid))
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
            future = fax_actor_ref.get.do_send_fax.remote(f.hashed_email, f.uuid)
            ray.get(future)
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
            future = fax_actor_ref.get.do_send_fax.remote(fax.hashed_email, fax.uuid)
            ray.get(future)
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
        f.save()
        fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)
        return True

    @classmethod
    def remote_send_fax(cls, hashed_email: str, uuid: str) -> bool:
        """
        Send a fax using ray non-blocking.

        Args:
            hashed_email: Hashed email for the fax
            uuid: UUID of the fax to send

        Returns:
            True if send was initiated
        """
        # Mark fax as to be sent just in case ray doesn't follow through
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        f.should_send = True
        f.paid = True
        f.save()
        fax_actor_ref.get.do_send_fax.remote(hashed_email, uuid)
        return True

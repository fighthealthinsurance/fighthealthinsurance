"""
Fax sending helpers for Fight Health Insurance.

Provides utilities for staging and sending appeal faxes.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

import ray

from loguru import logger

from fighthealthinsurance.fax_actor_ref import fax_actor_ref
from fighthealthinsurance.models import Appeal, Denial, FaxesToSend
from fighthealthinsurance.temporal_client import (
    dispatch_fax_send,
    dispatch_fax_send_blocking,
)

# NOTE: unlike every other per-task Ray dispatch on a request path, the fax
# dispatches below deliberately do NOT gate on base_actor_ref.ray_cluster_available().
# Elsewhere the gate is safe because skipping costs a background refresh; here it
# would cost a fax the user paid for. SendFaxHelper.redispatch_stranded_faxes
# picks up a lost dispatch, but only once an hour and from a Ray actor, so it is
# a backstop for the rare lost send, not a reason to skip one.
#
# Auto-initializing a local Ray cluster to send one fax is bad. Not sending it
# is worse.

# How long after a fax is staged the links that carry its (uuid,
# hashed_email) pair still act on it. The follow-up link reaches the person
# only in the email finalize_fax sends after each attempt, normally within an
# hour or two of staging (a delayed send waits an hour, and one vendor attempt
# can run 30 minutes). People answer a failed-fax or missing-number email
# within days. 30 days leaves room for a few weeks away from email, and for a
# few rounds of trying again, each of which emails the same link. Stripe's
# success link (fax_views.SendFaxView) is opened when a payment finishes, and
# a checkout stays open a day at most, so it is used well inside the window.
# Past it, the pages point the person to support.
FAX_RESEND_WINDOW = timedelta(days=30)

# Why SendFaxHelper.resend_refusal turns a re-send down.
RESEND_DELIVERED = "delivered"
RESEND_EXPIRED = "expired"
RESEND_IN_PROGRESS = "in_progress"

# How long an attempt that has not finished may still be running: the send
# step stops at 30 minutes and finalize follows, so this leaves room to
# spare. The staff fax queue calls an older one a stale attempt.
SEND_ATTEMPT_LIVE = timedelta(hours=2)

# Staging starts a send within seconds, so a request this old with no
# attempt never started sending.
REQUESTED_SEND_GRACE = timedelta(minutes=15)

# How recent a stranded fax redispatch_stranded_faxes sends on its own: long
# enough to ride out a weekend outage. An older request may no longer be
# wanted, so it waits on the staff fax queue for a person to decide.
REDISPATCH_WINDOW = timedelta(days=3)

# The most faxes one redispatch_stranded_faxes pass sends again.
REDISPATCH_BATCH = 50


def _no_send_running(now: datetime) -> Q:
    """Rows no send may still be running for: nothing holds the vendor-send
    claim, and the last attempt finished, none started, or it started more
    than SEND_ATTEMPT_LIVE ago. The update in SendFaxHelper.resend uses it;
    SendFaxHelper.send_in_progress is the same rule for a loaded row."""
    return Q(vendor_send_completed=False) & (
        Q(sent=True)
        | Q(attempting_to_send_as_of__isnull=True)
        | Q(attempting_to_send_as_of__lt=now - SEND_ATTEMPT_LIVE)
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
        try:
            fax_actor_ref.get.do_send_fax.remote(hashed_email, str(fax_uuid))
        except Exception:
            # First use of the handle; see BaseActorRef.invalidate.
            fax_actor_ref.invalidate()
            raise


def _blocking_dispatch_or_ray_fax(hashed_email: str, fax_uuid: str) -> None:
    """Send a fax and block until it finishes, via Temporal when enabled else Ray."""
    if dispatch_fax_send_blocking(hashed_email, str(fax_uuid)) is None:
        try:
            ray.get(fax_actor_ref.get.do_send_fax.remote(hashed_email, str(fax_uuid)))
        except Exception:
            fax_actor_ref.invalidate()
            raise


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
            # Asked to send, paid or not: how the staff page finds a failed dispatch.
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
    def resend_refusal(cls, fax: FaxesToSend) -> Optional[str]:
        """
        Why the follow-up link can't re-send this fax, or None when it can.

        A fax that went through stays sent (RESEND_DELIVERED), and a fax
        staged more than FAX_RESEND_WINDOW ago can't be re-sent from its
        link (RESEND_EXPIRED). While a send may still be running, the fax
        waits for it (RESEND_IN_PROGRESS). A failed fax, or one with no
        number, inside the window can be re-sent.
        """
        if fax.sent and fax.fax_success:
            return RESEND_DELIVERED
        if cls.link_expired(fax):
            return RESEND_EXPIRED
        if cls.send_in_progress(fax):
            return RESEND_IN_PROGRESS
        return None

    @classmethod
    def send_in_progress(cls, fax: FaxesToSend) -> bool:
        """Whether a send may still be running for this fax: one holds the
        vendor-send claim, or one started within SEND_ATTEMPT_LIVE and has
        not finished. The same rule as _no_send_running."""
        if fax.vendor_send_completed:
            return True
        started = fax.attempting_to_send_as_of
        return (
            not fax.sent
            and started is not None
            and timezone.now() - started < SEND_ATTEMPT_LIVE
        )

    @classmethod
    def link_expired(cls, fax: FaxesToSend) -> bool:
        """Whether the fax was staged more than FAX_RESEND_WINDOW ago, so the
        links that carry its pair no longer act on it."""
        return timezone.now() - fax.date > FAX_RESEND_WINDOW

    @classmethod
    def resend(cls, fax_phone: str, uuid: str, hashed_email: str) -> bool:
        """
        Resend a fax to a new phone number.

        Args:
            fax_phone: New destination fax number
            uuid: UUID of the fax to resend
            hashed_email: Hashed email for lookup

        Returns:
            True if resend was initiated, False when resend_refusal turns it
            down (the fax was delivered, is older than FAX_RESEND_WINDOW, or
            a send may still be running for it)
        """
        f = FaxesToSend.objects.filter(hashed_email=hashed_email, uuid=uuid).get()
        refusal = cls.resend_refusal(f)
        if refusal is not None:
            logger.info(f"Fax uuid={uuid} not re-sent: {refusal}")
            return False
        # One conditional update, so a send that started since the check
        # above, or a second submit, can't overlap this one.
        started = (
            FaxesToSend.objects.filter(pk=f.pk)
            .filter(_no_send_running(timezone.now()))
            .update(
                destination=fax_phone,
                should_send=True,
                sent=False,
                # The next precheck stamps it again; until then a lost
                # dispatch is one redispatch_stranded_faxes can find.
                attempting_to_send_as_of=None,
            )
        )
        if not started:
            logger.info(f"Fax uuid={uuid} not re-sent: {RESEND_IN_PROGRESS}")
            return False
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

    @classmethod
    def redispatch_stranded_faxes(cls) -> int:
        """Send again each fax whose dispatch was lost; returns how many.

        A fax someone asked to send (paid, or should_send) that is unsent,
        with no attempt started and no claim held, REQUESTED_SEND_GRACE after
        staging never reached a sender: its Temporal start failed and the Ray
        fallback failed or was lost. Inside REDISPATCH_WINDOW it goes through
        _dispatch_or_ray_fax, as the views send it, with whichever backend is
        on. The Temporal workflow id is the fax's own, so a run still open for
        it is left to finish. With Temporal off, should_send rows are left to
        the Ray delayed-fax sweep, which already sends them.
        """
        now = timezone.now()
        asked = Q(paid=True) | Q(should_send=True)
        if not getattr(settings, "TEMPORAL_ENABLED", False):
            asked = Q(paid=True, should_send=False)
        stranded = (
            FaxesToSend.objects.filter(asked)
            .filter(
                sent=False,
                attempting_to_send_as_of__isnull=True,
                vendor_send_completed=False,
                # Precheck stops on a fax with no denial without marking it,
                # so it would come back every pass.
                denial_id__isnull=False,
                date__lt=now - REQUESTED_SEND_GRACE,
                date__gte=now - REDISPATCH_WINDOW,
            )
            .order_by("date")
            .values_list("hashed_email", "uuid")[:REDISPATCH_BATCH]
        )
        dispatched = 0
        for hashed_email, fax_uuid in stranded:
            try:
                _dispatch_or_ray_fax(hashed_email, str(fax_uuid))
                dispatched += 1
            except Exception:
                logger.opt(exception=True).error(
                    f"Could not re-dispatch stranded fax uuid={fax_uuid}"
                )
        return dispatched

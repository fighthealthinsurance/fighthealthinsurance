"""
Fax sending helpers for Fight Health Insurance.

Provides utilities for staging and sending appeal faxes.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

import ray
from channels.db import database_sync_to_async

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
# would cost a fax the user paid for. SendFaxHelper.aredispatch_stranded_faxes
# picks up a lost dispatch, but only through Temporal and only after
# REQUESTED_SEND_GRACE, so it is a backstop for the rare lost send, not a
# reason to skip one.
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

# How recent a stranded fax aredispatch_stranded_faxes sends on its own: long
# enough to ride out a weekend outage. An older request may no longer be
# wanted, so it waits on the staff fax queue for a person to decide.
REDISPATCH_WINDOW = timedelta(days=3)

# The most faxes one aredispatch_stranded_faxes pass sends again.
REDISPATCH_BATCH = 50

# How long one Temporal connect, or one workflow start, may take in that pass.
REDISPATCH_START_TIMEOUT = timedelta(seconds=15)

# How long after a send starts its sender may still be running: the send
# step's 30-minute limit, SendFaxWorkflow's 35-minute ZOMBIE_DRAIN, and ten
# minutes to spare. The staff resend waits this long before it takes over.
STAFF_RESEND_AFTER = timedelta(minutes=75)


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
        health_history: Optional[str] = None,
    ) -> FaxHelperResults:
        """
        Stage an appeal to be sent as a fax.

        Args:
            appeal: The appeal to send
            email: Email address associated with the appeal
            professional: Whether this is from a professional user
            fax_number: Optional override fax number
            health_history: The health history the person chose to send,
                kept so a rebuilt document still includes it

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
            health_history=health_history,
            email=email,
            denial_id=denial,
            combined_document_enc=appeal.document_enc,
            destination=appeal_fax_number or fax_number,
            professional=professional,
        )
        # We call str on fts.uuid since it's a UUID object but when persisted it's a string
        _dispatch_or_ray_fax(fts.hashed_email, str(fts.uuid))
        # Linked only once a sender has the fax: stranded_faxes takes only
        # linked faxes, so one whose staging showed an error is never sent.
        try:
            appeal.fax = fts
            appeal.save()
        except Exception:
            logger.opt(exception=True).error(
                f"Could not link its appeal to fax uuid={fts.uuid}"
            )
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
        # One conditional update that also marks the attempt, so a send that
        # started since the check above, or a second submit before this one's
        # precheck, can't overlap it. Precheck marks it again as it runs.
        now = timezone.now()
        started = (
            FaxesToSend.objects.filter(pk=f.pk)
            .filter(_no_send_running(now))
            .update(
                destination=fax_phone,
                should_send=True,
                sent=False,
                attempting_to_send_as_of=now,
            )
        )
        if not started:
            logger.info(f"Fax uuid={uuid} not re-sent: {RESEND_IN_PROGRESS}")
            return False
        # force_restart supersedes any workflow still open for this fax so the
        # corrected destination is not dropped by a run already past its send
        # step (which would never re-read the new number).
        try:
            _dispatch_or_ray_fax(hashed_email, uuid, force_restart=True)
        except Exception:
            # Nothing started, so nothing is under way: unmark the attempt.
            FaxesToSend.objects.filter(pk=f.pk, attempting_to_send_as_of=now).update(
                attempting_to_send_as_of=None
            )
            raise
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
        # Only these fields, so a claim taken since the load isn't undone.
        f.save(update_fields=["should_send", "paid"])
        _dispatch_or_ray_fax(hashed_email, uuid)
        return "dispatched"

    @classmethod
    def stranded_faxes(cls) -> List[Tuple[str, str]]:
        """The (hashed_email, uuid) of each fax whose dispatch was lost,
        oldest first.

        A fax someone asked to send (paid, or should_send) that is unsent,
        with no attempt started and no claim held, REQUESTED_SEND_GRACE after
        staging never reached a sender: its Temporal start failed and the Ray
        fallback failed or was lost. Only faxes an appeal links (staging
        links it once a sender has the fax, so not one whose staging showed
        the person an error), staged inside REDISPATCH_WINDOW, and at most
        REDISPATCH_BATCH of them. A fax staged again for the same denial is
        left to the newer one, which is how a retry after an error looks,
        whichever code staged it.
        """
        now = timezone.now()
        stranded = (
            FaxesToSend.objects.filter(Q(paid=True) | Q(should_send=True))
            .filter(Exists(Appeal.objects.filter(fax=OuterRef("pk"))))
            .filter(
                ~Exists(
                    FaxesToSend.objects.filter(
                        denial_id=OuterRef("denial_id"), fax_id__gt=OuterRef("fax_id")
                    )
                )
            )
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
        return [(hashed_email, str(fax_uuid)) for hashed_email, fax_uuid in stranded]

    @classmethod
    async def aredispatch_stranded_faxes(cls) -> int:
        """Start a SendFaxWorkflow for each stranded fax; returns how many
        have a run, the ones already open included.

        Temporal only: a start uses the fax's own workflow id, so a fax that
        matches again on a later pass while its run is open starts nothing
        new. A Ray send has no such guard and could be queued twice, so with
        Temporal off nothing is sent, and a start that fails waits for the
        next pass.
        """
        if not getattr(settings, "TEMPORAL_ENABLED", False):
            return 0
        stranded = await database_sync_to_async(cls.stranded_faxes)()
        if not stranded:
            return 0
        return await _astart_fax_sends(stranded)


async def _astart_fax_sends(faxes: Iterable[Tuple[str, str]]) -> int:
    """Start a SendFaxWorkflow for each (hashed_email, uuid), each connect
    and start bounded by REDISPATCH_START_TIMEOUT; returns how many have a
    run. A run already open for a fax counts and is left alone."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from fighthealthinsurance.temporal_client import (
        get_temporal_client,
        start_send_fax_workflow,
    )

    limit = REDISPATCH_START_TIMEOUT.total_seconds()
    client = await asyncio.wait_for(get_temporal_client(), limit)
    held = 0
    for hashed_email, fax_uuid in faxes:
        try:
            await asyncio.wait_for(
                start_send_fax_workflow(
                    hashed_email,
                    fax_uuid,
                    client=client,
                    rpc_timeout=REDISPATCH_START_TIMEOUT,
                ),
                limit,
            )
        except WorkflowAlreadyStartedError:
            pass
        except Exception:
            logger.opt(exception=True).warning(
                f"Could not start a send for stranded fax uuid={fax_uuid}"
            )
            continue
        held += 1
    return held

"""Tests for fax_send_core precheck/resend semantics and the fax dispatch fallback.

These cover regressions found in the adversarial review of the Temporal fax
integration:

1. An explicit resend waits while a send may still be running (one holds
   the vendor-send claim, or one started and has not finished), under
   Temporal and Ray alike, so two sends of one fax never overlap.
2. The precheck already-sent guard must only skip *successful* sends; a failed
   attempt (sent=True, fax_success=False) stays retryable, matching the
   original FaxActor behavior.
3. A fax whose Temporal dispatch fails falls back to a Ray send, without
   first checking for a reachable Ray cluster: under Temporal the polling
   sweep is gated off, so nothing else would send it.
"""

import asyncio
import time

import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from asgiref.sync import async_to_sync

from django.test import override_settings
from django.utils import timezone

from fighthealthinsurance import fax_send_core
from fighthealthinsurance.fax_status import STATUS_ALREADY_SENT, STATUS_OK
from fighthealthinsurance.helpers.fax_helpers import (
    FAX_RESEND_WINDOW,
    REDISPATCH_WINDOW,
    REQUESTED_SEND_GRACE,
    RESEND_DELIVERED,
    RESEND_EXPIRED,
    RESEND_IN_PROGRESS,
    SEND_ATTEMPT_LIVE,
    SendFaxHelper,
    _dispatch_or_ray_fax,
)
from fighthealthinsurance.models import Appeal, Denial, FaxesToSend


@pytest.fixture
def test_denial(db):
    email = "fax_send_core_test@example.com"
    return Denial.objects.create(
        denial_text="Test denial for fax send core",
        hashed_email=Denial.get_hashed_email(email),
        raw_email=email,
        health_history="",
    )


def _make_fax(test_denial, **overrides):
    defaults = dict(
        hashed_email=test_denial.hashed_email,
        paid=True,
        email=test_denial.raw_email,
        name="Test User",
        appeal_text="Test appeal text",
        denial_id=test_denial,
        destination="4255551234",
        sent=False,
    )
    defaults.update(overrides)
    return FaxesToSend.objects.create(**defaults)


DISPATCH = "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"


def _resend(fax, fax_phone="8005559999"):
    return SendFaxHelper.resend(
        fax_phone=fax_phone, uuid=str(fax.uuid), hashed_email=fax.hashed_email
    )


@pytest.mark.django_db
class TestResendWaitsForASendUnderWay:
    """A resend starts only when no send may still be running for the fax,
    and leaves the claim to the send that holds it."""

    def test_resend_forces_workflow_restart(self, test_denial):
        """A resend must supersede any in-flight run so the corrected number
        isn't dropped by a run already past its send step."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        with patch(DISPATCH) as dispatch:
            assert _resend(fax) is True
        assert dispatch.call_args.kwargs.get("force_restart") is True
        fax.refresh_from_db()
        assert fax.destination == "8005559999"
        assert fax.sent is False and fax.should_send is True

    @override_settings(TEMPORAL_ENABLED=True)
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    @patch("fighthealthinsurance.helpers.fax_helpers.dispatch_fax_send")
    def test_a_claim_held_by_a_temporal_send_starts_no_second_send(
        self, mock_dispatch, mock_actor_ref, test_denial
    ):
        """A failed send whose run is still draining keeps its claim; the
        resend must not terminate that run or clear the claim."""
        fax = _make_fax(
            test_denial,
            sent=True,
            fax_success=False,
            vendor_send_completed=True,
            attempting_to_send_as_of=timezone.now() - timedelta(minutes=40),
        )
        assert _resend(fax) is False
        mock_dispatch.assert_not_called()
        mock_actor_ref.get.do_send_fax.remote.assert_not_called()
        fax.refresh_from_db()
        assert fax.vendor_send_completed is True
        assert fax.destination == "4255551234"

    @override_settings(TEMPORAL_ENABLED=False)
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    def test_a_claim_held_by_a_ray_send_starts_no_second_send(
        self, mock_actor_ref, test_denial
    ):
        fax = _make_fax(
            test_denial,
            vendor_send_completed=True,
            attempting_to_send_as_of=timezone.now() - timedelta(minutes=5),
        )
        assert _resend(fax) is False
        mock_actor_ref.get.do_send_fax.remote.assert_not_called()
        fax.refresh_from_db()
        assert fax.vendor_send_completed is True
        assert fax.destination == "4255551234"

    def test_a_second_submit_after_the_first_resend_started_sends_nothing(
        self, test_denial
    ):
        """Between precheck and the claim the document is being built; a
        second submit then must not start a second send."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        with patch(DISPATCH) as dispatch:
            assert _resend(fax) is True
            fax.refresh_from_db()
            fax_send_core.precheck_fax(fax)
            assert _resend(fax, fax_phone="8005550000") is False
        dispatch.assert_called_once()
        fax.refresh_from_db()
        assert fax.destination == "8005559999"

    def test_a_send_that_starts_after_the_check_wins_the_update(self, test_denial):
        """The update itself refuses, so a send that claims the fax between
        resend_refusal and the write still blocks the resend."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        FaxesToSend.objects.filter(pk=fax.pk).update(
            vendor_send_completed=True, attempting_to_send_as_of=timezone.now()
        )
        with patch.object(SendFaxHelper, "resend_refusal", return_value=None), patch(
            DISPATCH
        ) as dispatch:
            assert _resend(fax) is False
        dispatch.assert_not_called()
        fax.refresh_from_db()
        assert fax.destination == "4255551234"

    def test_an_attempt_older_than_a_live_send_no_longer_blocks(self, test_denial):
        fax = _make_fax(
            test_denial,
            attempting_to_send_as_of=timezone.now()
            - SEND_ATTEMPT_LIVE
            - timedelta(minutes=1),
        )
        with patch(DISPATCH) as dispatch:
            assert _resend(fax) is True
        dispatch.assert_called_once()

    def test_two_submits_before_any_precheck_start_one_send(self, test_denial):
        """The resend marks its attempt in the same update, so a second
        submit before the first send's precheck is turned away too."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        with patch(DISPATCH) as dispatch:
            assert _resend(fax) is True
            assert _resend(fax, fax_phone="8005550000") is False
        dispatch.assert_called_once()
        fax.refresh_from_db()
        assert fax.destination == "8005559999"
        assert fax.attempting_to_send_as_of is not None

    def test_a_dispatch_that_fails_unmarks_the_attempt(self, test_denial):
        """Nothing started, so the person, or the reconcile, can try again."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        with patch(DISPATCH, side_effect=RuntimeError("no backend")):
            with pytest.raises(RuntimeError):
                _resend(fax)
        fax.refresh_from_db()
        assert fax.attempting_to_send_as_of is None


@pytest.mark.django_db
class TestResendRefusals:
    """The follow-up link re-sends a failed fax, or one with no number, for
    FAX_RESEND_WINDOW after it was staged. A delivered fax stays sent."""

    def _resend(self, fax):
        with patch(
            "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"
        ) as dispatch:
            result = SendFaxHelper.resend(
                fax_phone="8005559999",
                uuid=str(fax.uuid),
                hashed_email=fax.hashed_email,
            )
        return result, dispatch

    def _staged_ago(self, fax, age):
        FaxesToSend.objects.filter(pk=fax.pk).update(date=timezone.now() - age)
        fax.refresh_from_db()

    def test_a_delivered_fax_is_not_resent(self, test_denial):
        fax = _make_fax(
            test_denial, sent=True, fax_success=True, vendor_send_completed=True
        )
        result, dispatch = self._resend(fax)
        assert result is False
        dispatch.assert_not_called()
        fax.refresh_from_db()
        assert fax.destination == "4255551234"
        assert fax.sent is True and fax.fax_success is True
        assert fax.vendor_send_completed is True

    def test_a_fax_past_the_window_is_not_resent(self, test_denial):
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        self._staged_ago(fax, FAX_RESEND_WINDOW + timedelta(minutes=1))
        result, dispatch = self._resend(fax)
        assert result is False
        dispatch.assert_not_called()
        fax.refresh_from_db()
        assert fax.destination == "4255551234"

    def test_a_failed_fax_inside_the_window_is_resent(self, test_denial):
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        self._staged_ago(fax, FAX_RESEND_WINDOW - timedelta(minutes=1))
        result, dispatch = self._resend(fax)
        assert result is True
        dispatch.assert_called_once()

    def test_refusal_reasons(self, test_denial):
        delivered = _make_fax(test_denial, sent=True, fax_success=True)
        old = _make_fax(test_denial, sent=True, fax_success=False)
        self._staged_ago(old, FAX_RESEND_WINDOW + timedelta(days=1))
        failed = _make_fax(test_denial, sent=True, fax_success=False)
        no_number = _make_fax(test_denial, destination=None)
        draining = _make_fax(
            test_denial,
            sent=True,
            fax_success=False,
            vendor_send_completed=True,
            attempting_to_send_as_of=timezone.now(),
        )
        assert SendFaxHelper.resend_refusal(delivered) == RESEND_DELIVERED
        assert SendFaxHelper.resend_refusal(old) == RESEND_EXPIRED
        assert SendFaxHelper.resend_refusal(failed) is None
        assert SendFaxHelper.resend_refusal(no_number) is None
        assert SendFaxHelper.resend_refusal(draining) == RESEND_IN_PROGRESS


@pytest.mark.django_db
class TestVendorSendAtomicClaim:
    """The vendor_send_completed marker must be claimed atomically so a
    concurrent second sender (Ray sweep vs Temporal timer, or a lost-ack Ray
    fallback) can't transmit the same appeal twice."""

    @patch("fighthealthinsurance.fax_send_core.flexible_fax_magic.send_fax")
    def test_concurrent_second_sender_does_not_retransmit(self, mock_send, test_denial):
        mock_send.return_value = True
        fax = _make_fax(test_denial)
        with patch.object(
            fax, "get_temporary_document_path", return_value="/tmp/does-not-exist.pdf"
        ):
            assert fax_send_core.send_fax_via_vendor(fax) == fax_send_core.SEND_OK
        fax.refresh_from_db()
        assert fax.vendor_send_completed is True

        # A racing sender that still holds a stale (False) in-memory marker must
        # lose the atomic claim and NOT call the vendor a second time. It gets
        # SEND_NOT_OWNER (the delivery isn't finalized yet) so it neither
        # finalizes nor releases the claim it does not hold.
        racer = FaxesToSend.objects.get(pk=fax.pk)
        racer.vendor_send_completed = False
        with patch.object(
            racer, "get_temporary_document_path", return_value="/tmp/does-not-exist.pdf"
        ):
            assert (
                fax_send_core.send_fax_via_vendor(racer) == fax_send_core.SEND_NOT_OWNER
            )
        assert mock_send.call_count == 1

    @patch("fighthealthinsurance.fax_send_core.flexible_fax_magic.send_fax")
    def test_failed_send_releases_claim_for_retry(self, mock_send, test_denial):
        mock_send.return_value = False
        fax = _make_fax(test_denial)
        with patch.object(
            fax, "get_temporary_document_path", return_value="/tmp/does-not-exist.pdf"
        ):
            assert fax_send_core.send_fax_via_vendor(fax) == fax_send_core.SEND_FAILED
        fax.refresh_from_db()
        # Claim released so a genuine retry can send.
        assert fax.vendor_send_completed is False


class _Both:
    def __init__(self, *cms):
        self._cms = cms

    def __enter__(self):
        return [cm.__enter__() for cm in self._cms]

    def __exit__(self, *exc):
        for cm in reversed(self._cms):
            cm.__exit__(*exc)
        return False


@pytest.mark.django_db
class TestFinalizeOrdering:
    """finalize must persist durable state before any notification, so an
    unlimited retry on a failing appeal.save() can't re-flood support."""

    def _pro_fax(self):
        fax = Mock()
        fax.pk = 1
        fax.professional = True
        fax.email = "pro@example.com"
        fax.uuid = "u"
        fax.for_appeal = Mock()
        return fax

    @staticmethod
    def _rows(transition: int, present: int):
        """finalize marks the fax with filtered UPDATEs (never save(), which
        could re-create a deleted row): the latest-attempt write
        (``filter(pk).update``, whose row count says whether the fax still
        exists), then the once-only counted markers
        (``filter(pk).filter(..._counted=False).update``). Model both, and
        keep the lifetime counter out of these ordering tests."""
        manager = MagicMock()
        manager.filter.return_value.filter.return_value.update.return_value = transition
        manager.filter.return_value.update.return_value = present
        return (
            patch("fighthealthinsurance.models.FaxesToSend.objects", manager),
            patch("fighthealthinsurance.lifetime_counters.note_fax_events"),
        )

    def _row_present(self):
        rows, counter = self._rows(transition=1, present=1)
        return _Both(rows, counter)

    def test_appeal_persisted_before_notification(self):
        fax = self._pro_fax()
        order: list = []
        fax.for_appeal.save.side_effect = lambda: order.append("appeal_save")
        with self._row_present(), patch(
            "fighthealthinsurance.fax_send_core.send_fax_status_notification",
            side_effect=lambda *a, **k: order.append("notify"),
        ):
            fax_send_core.finalize_fax(fax, True, False)
        assert order == ["appeal_save", "notify"]

    def test_no_notification_when_appeal_save_fails(self):
        fax = self._pro_fax()
        fax.for_appeal.save.side_effect = RuntimeError("locked appeal row")
        with self._row_present(), patch(
            "fighthealthinsurance.fax_send_core.send_fax_status_notification"
        ) as notify:
            with pytest.raises(RuntimeError, match="locked appeal row"):
                fax_send_core.finalize_fax(fax, True, False)
        # The appeal write was attempted and it is what failed; notification
        # comes after the durable writes, so a failed appeal.save() (which
        # the workflow will retry) never re-sends the support email.
        fax.for_appeal.save.assert_called_once()
        notify.assert_not_called()

    def test_gone_row_skips_the_appeal_write_and_notifications(self):
        fax = self._pro_fax()
        rows, counter = self._rows(transition=0, present=0)
        with rows, counter as bump, patch(
            "fighthealthinsurance.fax_send_core.send_fax_status_notification"
        ) as notify:
            fax_send_core.finalize_fax(fax, True, False)
        fax.for_appeal.save.assert_not_called()
        notify.assert_not_called()
        bump.assert_not_called()

    def test_already_delivered_row_is_not_counted_again(self):
        fax = self._pro_fax()
        rows, counter = self._rows(transition=0, present=1)
        with rows, counter as bump, patch(
            "fighthealthinsurance.fax_send_core.send_fax_status_notification"
        ):
            fax_send_core.finalize_fax(fax, True, False)
        bump.assert_called_once_with(newly_sent=False, newly_delivered=False)


@pytest.mark.django_db
class TestRemoteSendFaxConfirmation:
    """An already-sent fax reports 'already_sent' so the view can confirm
    receipt instead of a silent no-op that looks like a fresh send."""

    def test_already_sent_fax_is_confirmed_not_redispatched(self, test_denial):
        fax = _make_fax(test_denial, sent=True, fax_success=True)
        with patch(
            "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"
        ) as dispatch:
            result = SendFaxHelper.remote_send_fax(
                hashed_email=fax.hashed_email, uuid=str(fax.uuid)
            )
        assert result == "already_sent"
        dispatch.assert_not_called()

    def test_a_claim_taken_before_its_write_survives(self, test_denial):
        """Another sender claims the fax between this load and its write."""
        fax = _make_fax(test_denial)
        real_save = FaxesToSend.save

        def claimed_meanwhile(row, *args, **kwargs):
            FaxesToSend.objects.filter(pk=row.pk).update(vendor_send_completed=True)
            return real_save(row, *args, **kwargs)

        with patch.object(FaxesToSend, "save", claimed_meanwhile), patch(DISPATCH):
            SendFaxHelper.remote_send_fax(
                hashed_email=fax.hashed_email, uuid=str(fax.uuid)
            )
        fax.refresh_from_db()
        assert fax.vendor_send_completed is True
        assert fax.should_send is True

    def test_unsent_fax_is_dispatched(self, test_denial):
        fax = _make_fax(test_denial, sent=False)
        with patch(
            "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"
        ) as dispatch:
            result = SendFaxHelper.remote_send_fax(
                hashed_email=fax.hashed_email, uuid=str(fax.uuid)
            )
        assert result == "dispatched"
        dispatch.assert_called_once()


@pytest.mark.django_db
class TestPrecheckAlreadySentGuard:
    def test_precheck_allows_retry_of_failed_send(self, test_denial):
        """A failed attempt (sent=True, fax_success=False) stays retryable."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        assert fax_send_core.precheck_fax(fax) == STATUS_OK
        fax.refresh_from_db()
        assert fax.attempting_to_send_as_of is not None

    def test_precheck_skips_already_successful_send(self, test_denial):
        """A successful send is never re-attempted (duplicate-dispatch guard)."""
        fax = _make_fax(test_denial, sent=True, fax_success=True)
        assert fax_send_core.precheck_fax(fax) == STATUS_ALREADY_SENT

    @pytest.mark.parametrize(
        "overrides", [{"destination": None}, {"denial_id": None}], ids=str
    )
    def test_precheck_logs_the_fax_uuid_not_the_person(
        self, test_denial, log_capture, overrides
    ):
        fax = _make_fax(test_denial, name="Rebecca Lee Crumpler", **overrides)
        with log_capture() as cap, patch(
            "fighthealthinsurance.fax_send_core.send_fax_status_notification"
        ):
            fax_send_core.precheck_fax(fax)
        logged = " ".join(cap.messages("WARNING"))
        assert str(fax.uuid) in logged
        assert "Rebecca Lee Crumpler" not in logged
        assert fax.email not in logged

    def test_precheck_writes_back_nothing_it_loaded(self, test_denial):
        """A row loaded before a resend's new number, or another sender's
        claim, must not undo them when precheck marks its attempt."""
        fax = _make_fax(test_denial)
        stale = FaxesToSend.objects.select_related("denial_id").get(pk=fax.pk)
        FaxesToSend.objects.filter(pk=fax.pk).update(
            destination="8005559999", vendor_send_completed=True
        )
        assert fax_send_core.precheck_fax(stale) == STATUS_OK
        fax.refresh_from_db()
        assert fax.destination == "8005559999"
        assert fax.vendor_send_completed is True
        assert fax.attempting_to_send_as_of is not None


@pytest.mark.django_db
class TestFaxDispatchFallback:
    """Staging a fax, the Stripe success page and a resend all send through
    _dispatch_or_ray_fax, so its fallback is what keeps a fax from being
    stranded when Temporal cannot take it."""

    @override_settings(TEMPORAL_ENABLED=True)
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    @patch("fighthealthinsurance.helpers.fax_helpers.dispatch_fax_send")
    def test_a_failed_temporal_dispatch_falls_back_to_ray(
        self, mock_dispatch, mock_actor_ref, test_denial
    ):
        """Temporal on + dispatch failure must not orphan the fax."""
        mock_dispatch.return_value = False
        mock_actor_ref.get = MagicMock()
        fax = _make_fax(test_denial)

        _dispatch_or_ray_fax(fax.hashed_email, str(fax.uuid))

        mock_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            fax.hashed_email, str(fax.uuid)
        )

    @override_settings(TEMPORAL_ENABLED=True)
    @patch("fighthealthinsurance.base_actor_ref.ray_cluster_available")
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    @patch("fighthealthinsurance.helpers.fax_helpers.dispatch_fax_send")
    def test_a_fax_still_dispatches_with_no_ray_cluster(
        self, mock_dispatch, mock_actor_ref, mock_cluster, test_denial
    ):
        """The fax dispatches are deliberately NOT gated on a reachable cluster,
        unlike every other per-task Ray dispatch on a request path.

        Under TEMPORAL_ENABLED the delayed-fax sweep does not run at all --
        FaxPollingActor is never launched and send_delayed_faxes early-returns.
        The Ray fallback is reached exactly when the Temporal dispatch failed,
        so skipping it would strand the fax with nothing to retry it, and
        booting a local cluster is accepted as the lesser harm.

        ray_cluster_available is forced False (and asserted unconsulted) so
        re-adding the gate fails here rather than silently in production.
        """
        mock_cluster.return_value = False
        mock_dispatch.return_value = False
        mock_actor_ref.get = MagicMock()
        fax = _make_fax(test_denial)

        _dispatch_or_ray_fax(fax.hashed_email, str(fax.uuid))

        mock_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            fax.hashed_email, str(fax.uuid)
        )
        mock_cluster.assert_not_called()


TEMPORAL_START = "fighthealthinsurance.temporal_client.start_send_fax_workflow"
TEMPORAL_CONNECT = "fighthealthinsurance.temporal_client.get_temporal_client"
RAY = "fighthealthinsurance.helpers.fax_helpers.fax_actor_ref"


async def _hang(*args, **kwargs):
    await asyncio.Event().wait()


def _redispatch_pass():
    """One reconcile pass, bounded so a broken timeout fails the test
    rather than hanging the suite."""

    async def bounded():
        return await asyncio.wait_for(SendFaxHelper.aredispatch_stranded_faxes(), 10)

    return async_to_sync(bounded)()


@pytest.mark.django_db
class TestStrandedFaxes:
    """Which faxes count as stranded: asked for, unsent, never started."""

    def _stranded(
        self,
        test_denial,
        age=REQUESTED_SEND_GRACE + timedelta(minutes=1),
        linked=True,
        **overrides,
    ):
        """A fax as a finished staging leaves it, linked from its appeal."""
        fax = _make_fax(test_denial, **overrides)
        FaxesToSend.objects.filter(pk=fax.pk).update(date=timezone.now() - age)
        if linked:
            Appeal.objects.create(
                for_denial=test_denial,
                appeal_text="Test appeal text",
                hashed_email=test_denial.hashed_email,
                fax=fax,
            )
        return fax

    def _stage(self, test_denial, fails=False):
        """Stage a fax as StageFaxView does, each time with a new appeal."""
        appeal = Appeal.objects.create(
            for_denial=test_denial,
            appeal_text="Test appeal text",
            hashed_email=test_denial.hashed_email,
        )
        side_effect = RuntimeError("Temporal and Ray both down") if fails else None
        with patch(DISPATCH, side_effect=side_effect) as dispatch:
            staged = SendFaxHelper.stage_appeal_as_fax(
                appeal=appeal, email=test_denial.raw_email, fax_number="4255551234"
            )
        dispatch.assert_called_once()
        return staged

    def _past_the_grace(self):
        FaxesToSend.objects.update(
            date=timezone.now() - REQUESTED_SEND_GRACE - timedelta(minutes=1)
        )

    def test_a_staging_that_showed_an_error_is_never_sent_later(self, test_denial):
        with pytest.raises(RuntimeError):
            self._stage(test_denial, fails=True)
        self._past_the_grace()
        assert SendFaxHelper.stranded_faxes() == []

    @override_settings(TEMPORAL_ENABLED=True)
    def test_an_error_then_a_retry_sends_one_fax(self, test_denial):
        """The person sees an error and stages again; the retry's send
        starts, and no reconcile pass sends the first row as well."""
        with pytest.raises(RuntimeError):
            self._stage(test_denial, fails=True)
        # Staging again builds a new appeal, as StageFaxView does.
        retry = self._stage(test_denial)
        # The retry's send runs its precheck.
        fax_send_core.precheck_fax(FaxesToSend.objects.get(uuid=retry.uuid))
        self._past_the_grace()
        start = AsyncMock()
        with patch(TEMPORAL_CONNECT, AsyncMock()), patch(TEMPORAL_START, start), patch(
            RAY
        ) as ray_ref:
            assert _redispatch_pass() == 0
        start.assert_not_awaited()
        ray_ref.get.do_send_fax.remote.assert_not_called()

    def test_a_dispatch_lost_after_a_good_staging_is_still_found(self, test_denial):
        staged = self._stage(test_denial)
        self._past_the_grace()
        assert SendFaxHelper.stranded_faxes() == [(staged.hashed_email, staged.uuid)]

    def test_a_staged_fax_that_never_started_is_stranded(self, test_denial):
        fax = self._stranded(test_denial)
        assert SendFaxHelper.stranded_faxes() == [(fax.hashed_email, str(fax.uuid))]

    @pytest.mark.parametrize(
        "why, overrides",
        [
            (
                "still inside the grace",
                {"age": REQUESTED_SEND_GRACE - timedelta(minutes=1)},
            ),
            (
                "too old to send unasked",
                {"age": REDISPATCH_WINDOW + timedelta(hours=1)},
            ),
            ("an attempt started", {"attempting_to_send_as_of": timezone.now()}),
            ("a claim is held", {"vendor_send_completed": True}),
            ("already sent", {"sent": True, "fax_success": False}),
            ("never asked to send", {"paid": False, "should_send": False}),
            ("no denial to send for", {"denial_id": None}),
            ("its staging never finished", {"linked": False}),
        ],
    )
    def test_a_fax_that_is_not_stranded_is_left_alone(
        self, test_denial, why, overrides
    ):
        self._stranded(test_denial, **overrides)
        assert SendFaxHelper.stranded_faxes() == [], why

    @override_settings(TEMPORAL_ENABLED=True)
    def test_a_fax_is_started_once_however_many_passes_find_it(self, test_denial):
        """Pass one starts the run; pass two, while it is open, starts
        nothing new; once the run's precheck marks the attempt, no pass
        finds the fax. Ray is never asked, so nothing can queue twice."""
        from temporalio.exceptions import WorkflowAlreadyStartedError

        fax = self._stranded(test_denial)
        start = AsyncMock(
            side_effect=[
                f"send-fax-{fax.uuid}",
                WorkflowAlreadyStartedError(f"send-fax-{fax.uuid}", "SendFaxWorkflow"),
            ]
        )
        with patch(TEMPORAL_CONNECT, AsyncMock()), patch(TEMPORAL_START, start), patch(
            RAY
        ) as ray_ref:
            assert _redispatch_pass() == 1
            assert _redispatch_pass() == 1
            fax_send_core.precheck_fax(FaxesToSend.objects.get(pk=fax.pk))
            assert _redispatch_pass() == 0
        assert [c.args[:2] for c in start.await_args_list] == [
            (fax.hashed_email, str(fax.uuid))
        ] * 2
        ray_ref.get.do_send_fax.remote.assert_not_called()


@pytest.mark.django_db
class TestRedispatchStrandedFaxes:
    """The reconcile pass starts sends through Temporal only, each connect
    and start bounded, and never through Ray."""

    FAXES = [("h1", "u1"), ("h2", "u2")]

    def _pass(self, start, connect=None):
        with patch.object(
            SendFaxHelper, "stranded_faxes", return_value=self.FAXES
        ), patch(TEMPORAL_CONNECT, connect or AsyncMock()), patch(
            TEMPORAL_START, start
        ), patch(
            RAY
        ) as ray_ref:
            held = _redispatch_pass()
        return held, ray_ref

    @override_settings(TEMPORAL_ENABLED=False)
    def test_with_temporal_off_nothing_is_sent(self):
        start = AsyncMock()
        held, ray_ref = self._pass(start)
        assert held == 0
        start.assert_not_awaited()
        ray_ref.get.do_send_fax.remote.assert_not_called()

    @override_settings(TEMPORAL_ENABLED=True)
    def test_a_start_that_fails_never_falls_back_to_ray(self):
        start = AsyncMock(side_effect=RuntimeError("frontend unreachable"))
        held, ray_ref = self._pass(start)
        assert held == 0
        assert start.await_count == 2
        ray_ref.get.do_send_fax.remote.assert_not_called()

    @override_settings(TEMPORAL_ENABLED=True)
    def test_one_start_that_fails_does_not_stop_the_rest(self):
        start = AsyncMock(side_effect=[RuntimeError("blip"), "send-fax-u2"])
        held, _ = self._pass(start)
        assert held == 1
        assert start.await_count == 2

    @override_settings(TEMPORAL_ENABLED=True)
    @patch(
        "fighthealthinsurance.helpers.fax_helpers.REDISPATCH_START_TIMEOUT",
        timedelta(milliseconds=50),
    )
    def test_a_start_that_hangs_is_given_up_and_the_next_tried(self):
        start = AsyncMock(side_effect=_hang)
        began = time.monotonic()
        held, ray_ref = self._pass(start)
        assert time.monotonic() - began < 5
        assert held == 0
        assert start.await_count == 2
        ray_ref.get.do_send_fax.remote.assert_not_called()

    @override_settings(TEMPORAL_ENABLED=True)
    @patch(
        "fighthealthinsurance.helpers.fax_helpers.REDISPATCH_START_TIMEOUT",
        timedelta(milliseconds=50),
    )
    def test_a_connect_that_hangs_ends_the_pass(self):
        start = AsyncMock()
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            self._pass(start, connect=AsyncMock(side_effect=_hang))
        assert time.monotonic() - began < 5
        start.assert_not_awaited()

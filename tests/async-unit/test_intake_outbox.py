"""The intake outbox: append-only IntakeJourneyEvent rows, Temporal-first
delivery with Signal-With-Start, an independent relay with per-row backoff.
These are the process-death tests at both handoff boundaries that every
external review asked for, plus the review-7 regressions."""

import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from fighthealthinsurance import intake_outbox
from fighthealthinsurance.models import Denial, IntakeJourneyEvent

_INTAKE_ON = dict(
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=True,
    TEMPORAL_APPEAL_TASK_QUEUE="q-appeal",
)
_CLIENT = "fighthealthinsurance.temporal_client.get_temporal_client"


def _make_denial(denial_id, raw_email="person@example.com"):
    return Denial.objects.create(
        denial_id=denial_id,
        denial_text="Coverage for the requested MRI was denied.",
        semi_sekret="sekret",
        hashed_email=Denial.get_hashed_email(raw_email),
        raw_email=raw_email,
    )


def _client(start_side_effect=None, describe_side_effect=None):
    """A Temporal client stand-in recording start_workflow / describe calls."""
    client = Mock()
    client.start_workflow = AsyncMock(side_effect=start_side_effect)
    handle = Mock()
    handle.describe = AsyncMock(side_effect=describe_side_effect)
    client.get_workflow_handle = Mock(return_value=handle)
    return client


def _pending(denial, event, **extra):
    """An intent row as a committed mutation would have left it."""
    return IntakeJourneyEvent.objects.create(denial=denial, event_type=event, **extra)


def _reuse(kwargs):
    from temporalio.common import WorkflowIDReusePolicy

    return kwargs["id_reuse_policy"], WorkflowIDReusePolicy


@override_settings(**_INTAKE_ON)
class TestIntent(TransactionTestCase):
    def test_intent_commits_with_the_mutation_or_not_at_all(self):
        denial = _make_denial(8101)
        try:
            with transaction.atomic():
                denial.save()
                assert intake_outbox.record_intent(denial, intake_outbox.INTAKE_STARTED)
                raise RuntimeError("simulated failure after the save")
        except RuntimeError:
            pass
        assert not IntakeJourneyEvent.objects.filter(denial=denial).exists()

    def test_stale_denial_save_cannot_clobber_event_state(self):
        """Review-7 regression: outbox state must not live on Denial, where a
        full-row save() from a stale instance overwrites what another request
        set. Load a stale instance, ack via another path, save the stale one:
        the event is untouched."""
        stale = _make_denial(8102)
        stale = Denial.objects.get(pk=stale.pk)
        row = _pending(stale, intake_outbox.INTAKE_STARTED)
        IntakeJourneyEvent.objects.filter(pk=row.pk).update(acked_at=timezone.now())
        stale.denial_text = "edited by a slow request"
        stale.save()
        row.refresh_from_db()
        assert row.acked_at is not None


@override_settings(**_INTAKE_ON)
class TestDelivery(TransactionTestCase):
    def test_failed_delivery_never_raises_and_schedules_backoff(self):
        """Handoff #1 process death: the denial committed, Temporal was
        unreachable. The request must not fail; the row records a retry."""
        denial = _make_denial(8103)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(return_value=_client(ConnectionError("down")))):
            assert intake_outbox.deliver(row) is False
        row.refresh_from_db()
        assert row.acked_at is None
        assert row.attempts == 1
        assert row.last_error == "ConnectionError"
        assert row.last_error_at is not None
        assert row.next_attempt_at is not None
        assert row.next_attempt_at > timezone.now() + datetime.timedelta(seconds=20)

    def test_ack_failure_after_temporal_accepts_never_raises(self):
        """Finding 3: the whole post-commit path is one exception boundary.
        Temporal accepted but the ack UPDATE blows up: no exception reaches
        the request; the row stays pending for the relay."""
        denial = _make_denial(8104)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        real_filter = IntakeJourneyEvent.objects.filter

        def exploding_filter(*args, **kwargs):
            if "acked_at__isnull" in kwargs:
                raise RuntimeError("database went away during ack")
            return real_filter(*args, **kwargs)

        with patch(_CLIENT, AsyncMock(return_value=_client())):
            with patch.object(IntakeJourneyEvent.objects, "filter", exploding_filter):
                assert intake_outbox.deliver(row) is False
        row.refresh_from_db()
        assert row.acked_at is None

    def test_form_completed_uses_signal_with_start_allow_duplicate(self):
        """A closed (abandoned/failed) journey must not swallow completion:
        ALLOW_DUPLICATE starts a fresh run already-completed; a running one
        simply receives the signal -- same call either way."""
        denial = _make_denial(8105)
        row = _pending(denial, intake_outbox.FORM_COMPLETED)
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is True
        kwargs = client.start_workflow.call_args.kwargs
        policy, Policy = _reuse(kwargs)
        assert kwargs["id"] == f"intake-{denial.uuid}"
        assert kwargs["start_signal"] == "form_completed"
        assert policy == Policy.ALLOW_DUPLICATE
        row.refresh_from_db()
        assert row.acked_at is not None

    def test_form_completed_already_started_is_not_an_ack(self):
        from temporalio.exceptions import WorkflowAlreadyStartedError

        denial = _make_denial(8106)
        row = _pending(denial, intake_outbox.FORM_COMPLETED)
        err = WorkflowAlreadyStartedError(
            f"intake-{denial.uuid}", "IntakeJourneyWorkflow"
        )
        with patch(_CLIENT, AsyncMock(return_value=_client(err))):
            assert intake_outbox.deliver(row) is False
        row.refresh_from_db()
        assert row.acked_at is None and row.attempts == 1

    def test_intake_started_rejection_acks_only_when_execution_exists(self):
        from temporalio.exceptions import WorkflowAlreadyStartedError

        denial = _make_denial(8107)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        err = WorkflowAlreadyStartedError(
            f"intake-{denial.uuid}", "IntakeJourneyWorkflow"
        )
        client = _client(start_side_effect=err)
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is True
        policy, Policy = _reuse(client.start_workflow.call_args.kwargs)
        assert policy == Policy.REJECT_DUPLICATE
        client.get_workflow_handle.return_value.describe.assert_awaited_once()
        row.refresh_from_db()
        assert row.acked_at is not None

    def test_intake_started_rejection_without_execution_stays_pending(self):
        from temporalio.exceptions import WorkflowAlreadyStartedError

        denial = _make_denial(8108)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        err = WorkflowAlreadyStartedError(
            f"intake-{denial.uuid}", "IntakeJourneyWorkflow"
        )
        client = _client(
            start_side_effect=err, describe_side_effect=RuntimeError("not found")
        )
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is False
        row.refresh_from_db()
        assert row.acked_at is None and row.attempts == 1


@override_settings(**_INTAKE_ON)
class TestRelay(TransactionTestCase):
    def test_relay_delivers_pending_start_and_re_signals_lost_completion(self):
        """Both handoffs, and idempotency: a second pass is a no-op."""
        d1 = _make_denial(8109)
        d2 = _make_denial(8110)
        _pending(d1, intake_outbox.INTAKE_STARTED)
        _pending(d2, intake_outbox.INTAKE_STARTED, acked_at=timezone.now())
        _pending(d2, intake_outbox.FORM_COMPLETED)
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            first = intake_outbox.sweep()
            second = intake_outbox.sweep()
        assert first["delivered"] == 2 and first["backlog"] == 0
        assert second["delivered"] == 0 and second["failed"] == 0
        assert client.start_workflow.await_count == 2
        signals = [
            c.kwargs.get("start_signal") for c in client.start_workflow.call_args_list
        ]
        assert signals.count("form_completed") == 1
        assert not IntakeJourneyEvent.objects.filter(acked_at__isnull=True).exists()

    def test_backoff_doubles_and_caps(self):
        assert intake_outbox.backoff_seconds(1) == 30
        assert intake_outbox.backoff_seconds(2) == 60
        assert intake_outbox.backoff_seconds(3) == 120
        assert intake_outbox.backoff_seconds(20) == 3600

    def test_row_scheduled_for_later_is_not_retried_early(self):
        denial = _make_denial(8111)
        _pending(
            denial,
            intake_outbox.INTAKE_STARTED,
            attempts=3,
            next_attempt_at=timezone.now() + datetime.timedelta(minutes=5),
        )
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            counts = intake_outbox.sweep()
        client.start_workflow.assert_not_awaited()
        assert counts["delivered"] == 0 and counts["backlog"] == 1

    def test_poison_row_does_not_stop_later_rows(self):
        """Finding 3/4: one row whose delivery explodes (not a transport
        error -- a bug) is logged and skipped; the rows behind it deliver."""
        poison = _make_denial(8112)
        healthy = _make_denial(8113)
        prow = _pending(poison, intake_outbox.INTAKE_STARTED)
        _pending(healthy, intake_outbox.INTAKE_STARTED)
        real_deliver = intake_outbox.deliver

        def deliver_or_explode(row):
            if row.pk == prow.pk:
                raise RuntimeError("bug in delivery")
            return real_deliver(row)

        with patch(_CLIENT, AsyncMock(return_value=_client())):
            with patch.object(intake_outbox, "deliver", deliver_or_explode):
                counts = intake_outbox.sweep()
        assert counts["delivered"] == 1 and counts["failed"] == 1
        assert IntakeJourneyEvent.objects.get(denial=healthy).acked_at is not None

    def test_permanently_failing_row_does_not_starve_newer_rows(self):
        """A row that fails every time backs off; a newer row behind it is
        still delivered on the very next run."""
        old = _make_denial(8114)
        new = _make_denial(8115)
        _pending(old, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(return_value=_client(ConnectionError("down")))):
            intake_outbox.sweep()
        _pending(new, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(return_value=_client())):
            counts = intake_outbox.sweep()
        assert counts["delivered"] == 1
        assert IntakeJourneyEvent.objects.get(denial=new).acked_at is not None
        old_row = IntakeJourneyEvent.objects.get(denial=old)
        assert old_row.acked_at is None and old_row.attempts == 1

    def test_claim_skips_a_row_acked_between_selection_and_claim(self):
        """The claim re-checks acked/due under the row lock (SKIP LOCKED where
        the backend supports it), so a row another relay finished is skipped
        rather than delivered twice."""
        denial = _make_denial(8116)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        real_pending = intake_outbox._pending_pks

        def select_then_ack(limit):
            pks = real_pending(limit)
            IntakeJourneyEvent.objects.filter(pk=row.pk).update(acked_at=timezone.now())
            return pks

        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            with patch.object(intake_outbox, "_pending_pks", select_then_ack):
                counts = intake_outbox.sweep()
        client.start_workflow.assert_not_awaited()
        assert counts["skipped"] == 1

    def test_relay_is_inert_while_intake_is_dark(self):
        denial = _make_denial(8117)
        _pending(denial, intake_outbox.INTAKE_STARTED)
        client = _client()
        with override_settings(TEMPORAL_INTAKE_JOURNEY_ENABLED=False):
            with patch(_CLIENT, AsyncMock(return_value=client)):
                counts = intake_outbox.sweep()
                assert (
                    intake_outbox.record_intent(denial, intake_outbox.FORM_COMPLETED)
                    is None
                )
        assert counts["skipped_disabled"] == 1
        client.start_workflow.assert_not_awaited()
        assert IntakeJourneyEvent.objects.filter(denial=denial).count() == 1


@override_settings(**_INTAKE_ON)
class TestNudge(TransactionTestCase):
    def _send(self, denial):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance import intake_journey_core

        with patch.object(
            intake_journey_core, "_asend_mail", new_callable=AsyncMock
        ) as send:
            sent = async_to_sync(intake_journey_core.send_abandonment_nudge)(
                denial.hashed_email, str(denial.uuid)
            )
        return sent, send

    def test_nudge_claim_is_single_shot(self):
        denial = _make_denial(8118)
        first, send1 = self._send(denial)
        second, send2 = self._send(denial)
        assert first is True and send1.await_count == 1
        assert second is False and send2.await_count == 0
        assert (
            IntakeJourneyEvent.objects.filter(
                denial=denial, event_type=intake_outbox.NUDGE_SENT
            ).count()
            == 1
        )

    def test_nudge_skipped_when_form_completed(self):
        """Authoritative completion is the outbox record, not workflow state:
        a completed user never gets a 'you didn't finish' email even while
        the completion signal is still in flight."""
        denial = _make_denial(8119)
        _pending(denial, intake_outbox.FORM_COMPLETED)
        sent, send = self._send(denial)
        assert sent is False
        send.assert_not_awaited()

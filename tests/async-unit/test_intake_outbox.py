"""The intake outbox: append-only IntakeJourneyEvent rows, Temporal-first
delivery with Signal-With-Start, a two-phase relay (short locked claim, then
delivery with no lock held). These are the process-death tests at both
handoff boundaries that every external review asked for, plus the
review-7/8 regressions."""

import asyncio
import datetime
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from fighthealthinsurance import common_view_logic, intake_outbox
from fighthealthinsurance.models import Denial, IntakeJourneyEvent, PlanDocuments

_INTAKE_ON = dict(
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=True,
    TEMPORAL_APPEAL_TASK_QUEUE="q-appeal",
)
_CLIENT = "fighthealthinsurance.temporal_client.get_temporal_client"
_EMAIL = "person@example.com"


def _make_denial(denial_id, raw_email=_EMAIL):
    return Denial.objects.create(
        denial_id=denial_id,
        denial_text="Coverage for the requested MRI was denied as not medically necessary.",
        semi_sekret="sekret",
        hashed_email=Denial.get_hashed_email(raw_email),
        raw_email=raw_email,
        gen_attempts=3,
    )


def _client(start_side_effect=None, describe_side_effect=None, describe_status=None):
    """A Temporal client stand-in recording start_workflow / describe /
    signal calls. ``describe_status`` sets the described execution's status
    (a Mock, i.e. not RUNNING, when omitted)."""
    client = Mock()
    client.start_workflow = AsyncMock(side_effect=start_side_effect)
    handle = Mock()
    description = Mock()
    if describe_status is not None:
        description.status = describe_status
    handle.describe = AsyncMock(
        side_effect=describe_side_effect, return_value=description
    )
    handle.signal = AsyncMock()
    client.get_workflow_handle = Mock(return_value=handle)
    return client


def _pending(denial, event, **extra):
    """An intent row as a committed mutation would have left it."""
    return IntakeJourneyEvent.objects.create(denial=denial, event_type=event, **extra)


def _reuse(kwargs):
    from temporalio.common import WorkflowIDReusePolicy

    return kwargs["id_reuse_policy"], WorkflowIDReusePolicy


def _update_denial_owner():
    """The helper class that owns _update_denial (located by shape so the
    test does not pin a class name)."""
    for value in vars(common_view_logic).values():
        if isinstance(value, type) and "_update_denial" in vars(value):
            return value
    raise AssertionError("no class in common_view_logic defines _update_denial")


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

    def test_failing_intent_insert_rolls_back_plan_documents_too(self):
        """Review 8: plan documents, the denial update, and the intent are
        ONE transaction -- an intent failure rolls the documents back."""
        denial = _make_denial(8102)
        owner = _update_denial_owner()
        with patch(_CLIENT, AsyncMock(return_value=_client())):
            with patch.object(
                intake_outbox,
                "record_intent",
                side_effect=RuntimeError("insert failed"),
            ):
                with pytest.raises(RuntimeError):
                    owner._update_denial(denial, plan_documents=["plan text"])
        assert PlanDocuments.objects.filter(denial=denial).count() == 0
        assert not IntakeJourneyEvent.objects.filter(denial=denial).exists()

    def test_update_denial_commits_documents_denial_and_intent_together(self):
        denial = _make_denial(8103)
        owner = _update_denial_owner()
        with patch(_CLIENT, AsyncMock(return_value=_client())):
            owner._update_denial(
                denial, plan_documents=["plan text"], health_history="history"
            )
        assert PlanDocuments.objects.filter(denial=denial).count() == 1
        denial.refresh_from_db()
        assert denial.health_history == "history"
        row = IntakeJourneyEvent.objects.get(
            denial=denial, event_type=intake_outbox.INTAKE_STARTED
        )
        assert row.acked_at is not None

    def test_stale_denial_save_cannot_clobber_event_state(self):
        """Review-7 regression: outbox state must not live on Denial, where a
        full-row save() from a stale instance overwrites what another request
        set. Load a stale instance, ack via another path, save the stale one:
        the event is untouched."""
        stale = _make_denial(8104)
        stale = Denial.objects.get(pk=stale.pk)
        row = _pending(stale, intake_outbox.INTAKE_STARTED)
        IntakeJourneyEvent.objects.filter(pk=row.pk).update(acked_at=timezone.now())
        stale.denial_text = "edited by a slow request"
        stale.save()
        row.refresh_from_db()
        assert row.acked_at is not None


@override_settings(**_INTAKE_ON)
class TestFormCompletedRecordedEarly(TransactionTestCase):
    """Review 8 blocker: the FORM_COMPLETED intent must exist the moment the
    authenticated lookup succeeds -- before enrichment, research, RAG, or
    reserve logic can crash and make a completed user look abandoned."""

    def setUp(self):
        super().setUp()
        for target in (
            "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
            "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        ):
            patcher = patch(target, new_callable=AsyncMock, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        pmt_patcher = patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt"
        )
        pmt = pmt_patcher.start()
        pmt.find_context_for_denial = AsyncMock(return_value=None)
        self.addCleanup(pmt_patcher.stop)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_intent_exists_after_the_first_post_auth_yield(self, mock_gen):
        from asgiref.sync import async_to_sync

        denial = _make_denial(8105)
        mock_gen.make_appeals.return_value = iter([])

        async def drive():
            agen = common_view_logic.AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": denial.denial_id,
                    "email": _EMAIL,
                    "semi_sekret": denial.semi_sekret,
                }
            )
            try:
                await agen.__anext__()  # init status (pre-auth)
                await agen.__anext__()  # first post-authentication yield
            finally:
                await agen.aclose()

        with patch(_CLIENT, AsyncMock(return_value=_client())):
            async_to_sync(drive)()
        assert IntakeJourneyEvent.objects.filter(
            denial=denial, event_type=intake_outbox.FORM_COMPLETED
        ).exists()


@override_settings(**_INTAKE_ON)
class TestDelivery(TransactionTestCase):
    def test_failed_delivery_never_raises_and_schedules_backoff(self):
        """Handoff #1 process death: the denial committed, Temporal was
        unreachable. The request must not fail; the row records a retry."""
        denial = _make_denial(8106)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(return_value=_client(ConnectionError("down")))):
            assert intake_outbox.deliver(row) is False
        row.refresh_from_db()
        assert row.acked_at is None
        assert row.attempts == 1
        assert row.last_error == "ConnectionError"
        assert row.last_error_at is not None
        assert row.next_attempt_at > timezone.now() + datetime.timedelta(seconds=20)

    def test_ack_failure_after_temporal_accepts_never_raises(self):
        """Finding 3: the whole post-commit path is one exception boundary.
        Temporal accepted but the ack UPDATE blows up: no exception reaches
        the request; the row stays pending for the relay."""
        denial = _make_denial(8107)
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
        denial = _make_denial(8108)
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

        denial = _make_denial(8109)
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

        denial = _make_denial(8110)
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

        denial = _make_denial(8111)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        err = WorkflowAlreadyStartedError(
            f"intake-{denial.uuid}", "IntakeJourneyWorkflow"
        )
        client = _client(start_side_effect=err, describe_side_effect=RuntimeError("no"))
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is False
        row.refresh_from_db()
        assert row.acked_at is None and row.attempts == 1


@override_settings(**_INTAKE_ON)
class TestRelay(TransactionTestCase):
    def test_relay_delivers_pending_start_and_re_signals_lost_completion(self):
        d1 = _make_denial(8112)
        d2 = _make_denial(8113)
        _pending(d1, intake_outbox.INTAKE_STARTED)
        _pending(d2, intake_outbox.INTAKE_STARTED, acked_at=timezone.now())
        _pending(d2, intake_outbox.FORM_COMPLETED)
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            first = intake_outbox.sweep()
            second = intake_outbox.sweep()
        assert first["delivered"] == 2 and first["backlog"] == 0
        assert second["attempted"] == 0
        assert client.start_workflow.await_count == 2
        assert not IntakeJourneyEvent.objects.filter(
            acked_at__isnull=True, event_type__in=intake_outbox.DELIVERABLE_EVENTS
        ).exists()

    def test_one_temporal_client_per_batch(self):
        for i in range(3):
            _pending(_make_denial(8114 + i), intake_outbox.INTAKE_STARTED)
        connect = AsyncMock(return_value=_client())
        with patch(_CLIENT, connect):
            counts = intake_outbox.sweep()
        assert counts["delivered"] == 3
        assert connect.await_count == 1

    def test_claim_holds_no_lock_and_ack_is_conditional(self):
        """Review 8: claim first (short transaction, committed), deliver with
        no lock. A request-path delivery of the same event during the relay's
        window is NOT blocked (it acks), and the relay's conditional ack then
        matches zero rows instead of double-acking."""
        denial = _make_denial(8117)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        claims = intake_outbox.claim_batch()
        assert [pk for pk, _ in claims] == [row.pk]
        row.refresh_from_db()
        assert row.claimed_token is not None and row.claimed_until is not None
        # Another path acks the same event while the relay "is on the wire".
        with patch(_CLIENT, AsyncMock(return_value=_client())):
            assert intake_outbox.deliver(row) is True
        from asgiref.sync import async_to_sync

        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            counts = async_to_sync(intake_outbox.adeliver_claimed)(claims)
        assert counts["lost_claim"] == 1 and counts["delivered"] == 0
        client.start_workflow.assert_not_awaited()

    def test_conditional_ack_matches_zero_rows_when_claim_was_stolen(self):
        """The token, not the row, authorizes the ack: a claim re-issued to a
        newer run (expiry) means this run's ack must not land."""
        from asgiref.sync import async_to_sync

        denial = _make_denial(8118)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        stale = uuid.uuid4()
        IntakeJourneyEvent.objects.filter(pk=row.pk).update(
            claimed_token=uuid.uuid4(),
            claimed_until=timezone.now() + datetime.timedelta(minutes=2),
        )
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            counts = async_to_sync(intake_outbox.adeliver_claimed)([(row.pk, stale)])
        assert counts["lost_claim"] == 1
        client.start_workflow.assert_not_awaited()
        row.refresh_from_db()
        assert row.acked_at is None

    def test_expired_claim_is_eligible_again(self):
        denial = _make_denial(8119)
        row = _pending(
            denial,
            intake_outbox.INTAKE_STARTED,
            claimed_token=uuid.uuid4(),
            claimed_until=timezone.now() - datetime.timedelta(seconds=1),
        )
        old_token = row.claimed_token
        claims = intake_outbox.claim_batch()
        assert [pk for pk, _ in claims] == [row.pk]
        row.refresh_from_db()
        assert row.claimed_token != old_token
        assert row.claimed_until > timezone.now()

    def test_live_claim_is_skipped(self):
        denial = _make_denial(8120)
        _pending(
            denial,
            intake_outbox.INTAKE_STARTED,
            claimed_token=uuid.uuid4(),
            claimed_until=timezone.now() + datetime.timedelta(minutes=1),
        )
        assert intake_outbox.claim_batch() == []

    def test_backoff_doubles_and_caps(self):
        assert intake_outbox.backoff_seconds(1) == 30
        assert intake_outbox.backoff_seconds(2) == 60
        assert intake_outbox.backoff_seconds(3) == 120
        assert intake_outbox.backoff_seconds(20) == 3600

    def test_row_scheduled_for_later_is_not_retried_early(self):
        denial = _make_denial(8121)
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
        assert counts["attempted"] == 0 and counts["backlog"] == 1
        assert counts["oldest_pending_seconds"] >= 0

    def test_poison_row_does_not_stop_later_rows(self):
        """One row whose delivery explodes (a bug, not a transport error) is
        logged and skipped; the rows behind it deliver."""
        poison = _make_denial(8122)
        healthy = _make_denial(8123)
        _pending(poison, intake_outbox.INTAKE_STARTED)
        _pending(healthy, intake_outbox.INTAKE_STARTED)
        real_call = intake_outbox._acall

        async def call_or_explode(denial, event, client=None):
            if denial.pk == poison.pk:
                raise RuntimeError("bug in delivery")
            return await real_call(denial, event, client=client)

        with patch(_CLIENT, AsyncMock(return_value=_client())):
            with patch.object(intake_outbox, "_acall", call_or_explode):
                counts = intake_outbox.sweep()
        assert counts["delivered"] == 1 and counts["failed"] == 1
        assert counts["systemic"] is False  # a bug is not a transport outage
        assert IntakeJourneyEvent.objects.get(denial=healthy).acked_at is not None

    def test_permanently_failing_row_does_not_starve_newer_rows(self):
        old = _make_denial(8124)
        new = _make_denial(8125)
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
        assert old_row.claimed_token is None  # released with the backoff

    def test_time_budget_defers_unstarted_rows(self):
        for i in range(2):
            _pending(_make_denial(8126 + i), intake_outbox.INTAKE_STARTED)
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            counts = intake_outbox.sweep(time_budget=-1)
        assert counts["deferred"] == 2 and counts["attempted"] == 0
        client.start_workflow.assert_not_awaited()

    def test_per_call_timeout_is_a_transport_failure(self):
        denial = _make_denial(8128)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        client = _client(start_side_effect=hang)
        with patch(_CLIENT, AsyncMock(return_value=client)):
            with patch.object(intake_outbox, "RPC_TIMEOUT_SECONDS", 0.05):
                counts = intake_outbox.sweep()
        assert counts["failed"] == 1 and counts["systemic"] is True
        row.refresh_from_db()
        assert row.last_error == "TimeoutError"

    def test_relay_is_inert_while_intake_is_dark(self):
        denial = _make_denial(8129)
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
class TestRelayCommand(TransactionTestCase):
    def _run(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("deliver_intake_events", stdout=out)
        return out.getvalue()

    def test_rows_waiting_on_backoff_exit_zero(self):
        denial = _make_denial(8130)
        _pending(denial, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(return_value=_client(ConnectionError("down")))):
            # First run: every attempt fails at the transport level -> systemic.
            from django.core.management.base import CommandError

            with pytest.raises(CommandError):
                self._run()
        # Second run: the row is now waiting on backoff, nothing attempted.
        with patch(_CLIENT, AsyncMock(return_value=_client())):
            out = self._run()
        assert "backlog=1" in out and "attempted=0" in out

    def test_client_connect_failure_exits_non_zero(self):
        from django.core.management.base import CommandError

        denial = _make_denial(8131)
        _pending(denial, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(side_effect=ConnectionError("no temporal"))):
            with pytest.raises(CommandError):
                self._run()
        row = IntakeJourneyEvent.objects.get(denial=denial)
        assert row.acked_at is None  # claim expires; next run re-delivers

    def test_run_logs_backlog_and_counts(self):
        denial = _make_denial(8132)
        _pending(denial, intake_outbox.INTAKE_STARTED)
        with patch(_CLIENT, AsyncMock(return_value=_client())):
            out = self._run()
        assert "delivered=1" in out and "backlog=0" in out
        assert "oldest_pending_seconds=" in out


@override_settings(**_INTAKE_ON)
class TestNudge(TransactionTestCase):
    def _send(self, denial, side_effect=None):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance import intake_journey_core

        with patch.object(
            intake_journey_core,
            "_asend_mail",
            new_callable=AsyncMock,
            side_effect=side_effect,
        ) as send:
            sent = async_to_sync(intake_journey_core.send_abandonment_nudge)(
                denial.hashed_email, str(denial.uuid)
            )
        return sent, send

    def _claim(self, denial):
        return IntakeJourneyEvent.objects.get(
            denial=denial, event_type=intake_outbox.NUDGE_CLAIMED
        )

    def test_nudge_claim_is_single_shot(self):
        denial = _make_denial(8133)
        first, send1 = self._send(denial)
        second, send2 = self._send(denial)
        assert first is True and send1.await_count == 1
        assert second is False and send2.await_count == 0
        claim = self._claim(denial)
        assert claim.outcome == intake_outbox.OUTCOME_SENT
        assert claim.sent_at is not None and claim.attempted_at is not None

    def test_nudge_skipped_when_form_completed(self):
        denial = _make_denial(8134)
        _pending(denial, intake_outbox.FORM_COMPLETED)
        sent, send = self._send(denial)
        assert sent is False
        send.assert_not_awaited()
        claim = self._claim(denial)
        assert claim.outcome == intake_outbox.OUTCOME_SKIPPED_COMPLETED
        assert claim.sent_at is None

    def test_ambiguous_smtp_failure_keeps_the_claim(self):
        """The provider may have accepted the message: never release the
        claim, record the failure, and a retry sends nothing."""
        denial = _make_denial(8135)
        with pytest.raises(RuntimeError):
            self._send(denial, side_effect=RuntimeError("smtp hiccup"))
        claim = self._claim(denial)
        assert claim.outcome == intake_outbox.OUTCOME_SMTP_FAILED
        assert claim.sent_at is None
        again, send = self._send(denial)
        assert again is False
        send.assert_not_awaited()


@override_settings(**_INTAKE_ON)
class TestContactOptIn(TransactionTestCase):
    def test_retry_with_running_execution_signals_current_opt_in_then_acks(self):
        """intake_started rejected because a journey is already RUNNING: it
        was started with an older opt-in, so the current value is signalled
        before the delivery counts as acknowledged."""
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.exceptions import WorkflowAlreadyStartedError

        denial = _make_denial(8136)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        err = WorkflowAlreadyStartedError(
            f"intake-{denial.uuid}", "IntakeJourneyWorkflow"
        )
        client = _client(
            start_side_effect=err, describe_status=WorkflowExecutionStatus.RUNNING
        )
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is True
        handle = client.get_workflow_handle.return_value
        handle.signal.assert_awaited_once()
        args, kwargs = handle.signal.call_args
        assert args[0] == "contact_opt_in" and args[1] is True
        assert "rpc_timeout" in kwargs
        row.refresh_from_db()
        assert row.acked_at is not None

    def test_retry_with_closed_execution_acks_without_signalling(self):
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.exceptions import WorkflowAlreadyStartedError

        denial = _make_denial(8137)
        row = _pending(denial, intake_outbox.INTAKE_STARTED)
        err = WorkflowAlreadyStartedError(
            f"intake-{denial.uuid}", "IntakeJourneyWorkflow"
        )
        client = _client(
            start_side_effect=err, describe_status=WorkflowExecutionStatus.COMPLETED
        )
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is True
        client.get_workflow_handle.return_value.signal.assert_not_awaited()

    def test_opt_in_change_after_ack_signals_best_effort(self):
        denial = _make_denial(8138)
        _pending(denial, intake_outbox.INTAKE_STARTED, acked_at=timezone.now())
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.signal_contact_opt_in(denial) is True
        args, _ = client.get_workflow_handle.return_value.signal.call_args
        assert args[0] == "contact_opt_in" and args[1] is True

    def test_opt_in_change_signal_failure_never_raises(self):
        denial = _make_denial(8139)
        _pending(denial, intake_outbox.INTAKE_STARTED, acked_at=timezone.now())
        client = _client()
        client.get_workflow_handle.return_value.signal = AsyncMock(
            side_effect=ConnectionError("temporal down")
        )
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.signal_contact_opt_in(denial) is False

    def test_opt_in_change_before_any_acked_start_is_a_noop(self):
        denial = _make_denial(8140)
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.signal_contact_opt_in(denial) is False
        client.get_workflow_handle.assert_not_called()

    def test_start_calls_carry_a_bounded_rpc_timeout(self):
        denial = _make_denial(8141)
        row = _pending(denial, intake_outbox.FORM_COMPLETED)
        client = _client()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            assert intake_outbox.deliver(row) is True
        kwargs = client.start_workflow.call_args.kwargs
        assert kwargs["rpc_timeout"].total_seconds() == 15


@override_settings(**_INTAKE_ON)
class TestRelayLimit(TransactionTestCase):
    def test_negative_limit_is_a_command_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("deliver_intake_events", "--limit", "-1")

    def test_negative_limit_is_rejected_by_sweep(self):
        with pytest.raises(ValueError):
            intake_outbox.sweep(limit=-1)

    def test_zero_limit_is_an_empty_run(self):
        from io import StringIO

        from django.core.management import call_command

        denial = _make_denial(8142)
        _pending(denial, intake_outbox.INTAKE_STARTED)
        client = _client()
        out = StringIO()
        with patch(_CLIENT, AsyncMock(return_value=client)):
            call_command("deliver_intake_events", "--limit", "0", stdout=out)
        client.start_workflow.assert_not_awaited()
        assert "attempted=0" in out.getvalue() and "backlog=1" in out.getvalue()

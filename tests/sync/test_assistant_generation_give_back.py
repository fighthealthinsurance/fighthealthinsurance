"""A chat-path run that ends with no letters gives its reserved generation
back (assistant_drafts.give_back_generation, through the activities)."""

from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import assistant_drafts as drafts
from fighthealthinsurance.activities import assistant_appeal as activities
from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import (
    AssistantDraft,
    Denial,
    SpendCounter,
    SpendReservation,
)

ALL_ON = dict(
    MCP_DRAFT_IN_CHAT_ENABLED=True,
    MCP_SERVER_ENABLED=True,
    MCP_PREPARE_APPEAL_ENABLED=True,
    MCP_HANDOFF_V2_ENABLED=True,
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_PAYLOAD_KEY="test-key",
    FHI_SPEND_BACKGROUND=False,
)
TAKEN = "fhi:assistant"


def a_denial():
    return Denial.objects.create(
        hashed_email=Denial.get_hashed_email("person@example.com"),
        denial_text="The MRI was denied as not medically necessary.",
        insurance_company="Example Health",
        channel="assistant",
    )


def taken_today():
    return SpendCounter.objects.get(name=TAKEN, day=spend._today()).amount


def agreed_draft(reserve=True):
    """A draft agreed to the way the terms page does it, and its denial."""
    denial = a_denial()
    draft = drafts.create_draft(None).draft
    reservation = spend.reserve_generation() if reserve else None
    drafts.agree(
        draft, denial, reservation_id=reservation.id if reservation else None
    )
    return denial, draft


class GiveBackTestBase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)
        # Another run's generation, so a second release would show.
        self.assertIsNotNone(spend.reserve_generation())

    def finish(self, denial):
        return async_to_sync(activities.finish_drafts)(
            denial.hashed_email, str(denial.uuid)
        )

    def mark(self, denial, status):
        return async_to_sync(activities.mark_draft_status)(
            denial.hashed_email, str(denial.uuid), status
        )

    def released(self, draft):
        draft.refresh_from_db()
        return draft.spend_reservation.released_at is not None


class FinishDraftsTest(GiveBackTestBase):
    def test_a_run_that_stored_no_letters_gives_its_generation_back(self):
        denial, draft = agreed_draft()
        self.assertEqual(taken_today(), 2)
        self.assertEqual(self.finish(denial), drafts.STOPPED)
        self.assertTrue(self.released(draft))
        self.assertEqual(taken_today(), 1)

    def test_a_run_with_letters_keeps_its_generation(self):
        denial, draft = agreed_draft()
        with patch.object(drafts, "letters_status", return_value=drafts.READY):
            self.assertEqual(self.finish(denial), drafts.READY)
        self.assertFalse(self.released(draft))
        self.assertEqual(taken_today(), 2)

    def test_a_generation_the_site_took_keeps_its_generation(self):
        denial, draft = agreed_draft()
        with patch.object(drafts, "site_took_generation", return_value=True):
            self.assertEqual(self.finish(denial), drafts.ON_SITE)
        self.assertFalse(self.released(draft))
        self.assertEqual(taken_today(), 2)

    def test_a_retried_finish_gives_back_once(self):
        denial, draft = agreed_draft()
        self.finish(denial)
        self.finish(denial)
        self.assertEqual(taken_today(), 1)

    def test_a_failed_release_fails_the_activity_and_its_retry_gives_back(self):
        denial, draft = agreed_draft()
        with patch.object(spend, "release_generation", return_value=False):
            with self.assertRaises(ApplicationError):
                self.finish(denial)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.STOPPED)
        self.assertEqual(taken_today(), 2)
        self.assertEqual(self.finish(denial), drafts.STOPPED)
        self.assertEqual(taken_today(), 1)


class MarkDraftStatusTest(GiveBackTestBase):
    def test_stopped_gives_the_generation_back(self):
        denial, draft = agreed_draft()
        self.assertTrue(self.mark(denial, drafts.STOPPED))
        self.assertTrue(self.released(draft))
        self.assertEqual(taken_today(), 1)

    def test_expired_gives_the_generation_back(self):
        denial, draft = agreed_draft()
        self.assertTrue(self.mark(denial, drafts.EXPIRED))
        self.assertTrue(self.released(draft))
        self.assertEqual(draft.status, drafts.EXPIRED)
        self.assertEqual(taken_today(), 1)

    def test_expired_after_the_site_generated_keeps_the_generation(self):
        denial, draft = agreed_draft()
        with patch.object(drafts, "site_took_generation", return_value=True):
            self.mark(denial, drafts.EXPIRED)
        self.assertFalse(self.released(draft))
        self.assertEqual(taken_today(), 2)

    def test_stopped_with_letters_stored_keeps_the_generation(self):
        denial, draft = agreed_draft()
        with patch.object(drafts, "collect_letters", return_value=[{"text": "x"}]):
            self.mark(denial, drafts.STOPPED)
        self.assertFalse(self.released(draft))
        self.assertEqual(taken_today(), 2)

    def test_ready_keeps_the_generation(self):
        denial, draft = agreed_draft()
        self.mark(denial, drafts.READY)
        self.assertFalse(self.released(draft))
        self.assertEqual(taken_today(), 2)

    def test_a_retried_mark_gives_back_once(self):
        denial, draft = agreed_draft()
        self.mark(denial, drafts.EXPIRED)
        self.mark(denial, drafts.EXPIRED)
        self.assertEqual(taken_today(), 1)

    def test_an_old_draft_without_a_reservation_gives_nothing_back(self):
        denial, draft = agreed_draft(reserve=False)
        self.assertTrue(self.mark(denial, drafts.STOPPED))
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.STOPPED)
        self.assertEqual(taken_today(), 1)
        self.assertFalse(
            SpendReservation.objects.filter(released_at__isnull=False).exists()
        )

    def test_the_day_it_was_taken_from_is_credited(self):
        denial, draft = agreed_draft()
        yesterday = spend._today() - timedelta(days=1)
        SpendCounter.objects.create(day=yesterday, name=TAKEN, amount=5)
        SpendReservation.objects.filter(pk=draft.spend_reservation_id).update(
            day=yesterday
        )
        self.mark(denial, drafts.STOPPED)
        self.assertEqual(SpendCounter.objects.get(day=yesterday).amount, 4)
        self.assertEqual(taken_today(), 2)


@override_settings(FHI_SPEND_BACKGROUND=False)
class SweepGivesBackTest(TestCase):
    def setUp(self):
        super().setUp()
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)

    def _expired(self, status):
        _, draft = agreed_draft()
        AssistantDraft.objects.filter(pk=draft.pk).update(
            status=status, expires_at=timezone.now() - timedelta(seconds=1)
        )
        return SpendReservation.objects.get(pk=draft.spend_reservation_id)

    def test_a_swept_draft_still_waiting_for_answers_gives_back(self):
        reservation = self._expired(drafts.QUESTIONS)
        self.assertEqual(drafts.sweep_expired(), 1)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.released_at)
        self.assertEqual(taken_today(), 0)

    def test_a_swept_draft_that_reached_drafting_keeps_its_generation(self):
        reservation = self._expired(drafts.DRAFTING)
        self.assertEqual(drafts.sweep_expired(), 1)
        reservation.refresh_from_db()
        self.assertIsNone(reservation.released_at)
        self.assertEqual(taken_today(), 1)

    def test_a_swept_draft_the_site_generated_for_keeps_its_generation(self):
        reservation = self._expired(drafts.QUESTIONS)
        with patch.object(drafts, "site_took_generation", return_value=True):
            self.assertEqual(drafts.sweep_expired(), 1)
        reservation.refresh_from_db()
        self.assertIsNone(reservation.released_at)
        self.assertEqual(taken_today(), 1)

    def test_a_stopped_draft_whose_release_failed_is_given_back_by_the_sweep(self):
        reservation = self._expired(drafts.STOPPED)
        self.assertEqual(drafts.sweep_expired(), 1)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.released_at)
        self.assertEqual(taken_today(), 0)

    def test_a_failed_release_keeps_the_draft_for_the_next_sweep(self):
        reservation = self._expired(drafts.QUESTIONS)
        with patch.object(spend, "release_generation", return_value=False):
            self.assertEqual(drafts.sweep_expired(), 0)
        self.assertEqual(drafts.sweep_expired(), 1)
        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.released_at)

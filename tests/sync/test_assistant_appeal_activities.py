"""The chat path's Temporal activities, run for real against the database
(activities/assistant_appeal.py). The workflow tests in tests/temporal stub
these; here only the model calls are stubbed."""

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync, sync_to_async
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import assistant_drafts as drafts
from fighthealthinsurance import generation_lease, intake_outbox
from fighthealthinsurance.activities import assistant_appeal as activities
from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import (
    AssistantDraft,
    Denial,
    IntakeJourneyEvent,
    ProposedAppeal,
)

ALL_ON = dict(
    MCP_DRAFT_IN_CHAT_ENABLED=True,
    MCP_SERVER_ENABLED=True,
    MCP_PREPARE_APPEAL_ENABLED=True,
    MCP_HANDOFF_V2_ENABLED=True,
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=True,
    TEMPORAL_PAYLOAD_KEY="test-key",
    FHI_SPEND_BACKGROUND=False,
)
LETTER = (
    "Dear Example Health, I am writing to appeal the denial of my MRI. "
    "The scan is medically necessary for my condition and the treatment "
    "plan my doctor set out. Please reverse the decision. Sincerely, "
    "Rebecca Lee Crumpler"
)
QUESTIONS = [("What happened?", ""), ("Was it inpatient or outpatient?", "")]
# Far past the bound each test sets, so a missing bound shows as a slow test.
HANG_S = 5
BOUND_S = 0.05


async def finds_nothing(denial_id):
    """extract_entity that reads the letter and finds no procedure or
    diagnosis."""
    return
    yield


async def hangs(*args, **kwargs):
    await asyncio.sleep(HANG_S)
    return
    yield


async def asks_nothing_in_time(denial_id):
    await asyncio.sleep(HANG_S)
    return QUESTIONS


def agreed_draft(procedure="MRI", condition="back pain"):
    """A draft agreed to the way the terms page does it, and its denial."""
    denial = Denial.objects.create(
        hashed_email=Denial.get_hashed_email("person@example.com"),
        denial_text="The MRI was denied as not medically necessary.",
        insurance_company="Example Health",
        channel="assistant",
    )
    draft = drafts.create_draft(None).draft
    drafts.agree(draft, denial, procedure, condition)
    return denial, draft


def stopped_draft():
    """An agreed draft Agree then stopped when its wait on Temporal ran
    out, though Temporal started the run all the same."""
    denial, draft = agreed_draft()
    drafts.set_status(draft, drafts.STOPPED)
    draft.refresh_from_db()
    return denial, draft


class ActivityTestBase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)

    def run_activity(self, activity, denial, *args):
        return async_to_sync(activity)(denial.hashed_email, str(denial.uuid), *args)

    def assert_still_stopped(self, draft):
        """Stopped, and not set again since."""
        stopped_at = draft.status_at
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.status_at), (drafts.STOPPED, stopped_at))


class ReadLetterTest(ActivityTestBase):
    def read(self, denial, extract=finds_nothing):
        with patch.object(DenialCreatorHelper, "extract_entity", extract):
            return self.run_activity(activities.read_letter, denial)

    def test_what_the_reading_left_empty_is_filled_from_the_draft(self):
        denial, draft = agreed_draft()
        self.assertTrue(self.read(denial))
        denial.refresh_from_db()
        self.assertEqual((denial.procedure, denial.diagnosis), ("MRI", "back pain"))
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.READING)

    def test_what_the_reading_found_is_kept(self):
        denial, _ = agreed_draft()

        async def finds_a_procedure(denial_id):
            await Denial.objects.filter(denial_id=denial_id).aupdate(
                procedure="CT scan"
            )
            yield "procedure"

        self.read(denial, finds_a_procedure)
        denial.refresh_from_db()
        self.assertEqual((denial.procedure, denial.diagnosis), ("CT scan", "back pain"))

    def test_a_reading_that_runs_out_of_time_goes_on_with_the_draft(self):
        denial, _ = agreed_draft()
        started = time.monotonic()
        with patch.object(activities, "READ_TIMEOUT_S", BOUND_S):
            self.assertTrue(self.read(denial, hangs))
        self.assertLess(time.monotonic() - started, HANG_S / 2)
        denial.refresh_from_db()
        self.assertEqual((denial.procedure, denial.diagnosis), ("MRI", "back pain"))

    def test_no_such_case_is_false(self):
        denial, _ = agreed_draft()
        denial.hashed_email = Denial.get_hashed_email("someone@example.com")
        self.assertFalse(self.read(denial))

    def test_a_draft_stopped_after_its_check_is_not_read(self):
        denial, draft = stopped_draft()
        extract = MagicMock(side_effect=finds_nothing)
        with patch.object(activities, "_stopped", return_value=False):
            self.assertFalse(self.read(denial, extract))
        extract.assert_not_called()
        self.assert_still_stopped(draft)

    def test_a_run_that_finds_its_draft_stopped_reads_nothing_and_changes_nothing(
        self,
    ):
        denial, draft = stopped_draft()
        found_before = (denial.procedure, denial.diagnosis)
        extract = MagicMock(side_effect=finds_nothing)
        self.assertFalse(self.read(denial, extract))
        extract.assert_not_called()
        self.assert_still_stopped(draft)
        denial.refresh_from_db()
        self.assertEqual((denial.procedure, denial.diagnosis), found_before)


class AskQuestionsTest(ActivityTestBase):
    def ask(self, denial, generate):
        with patch.object(DenialCreatorHelper, "generate_appeal_questions", generate):
            return self.run_activity(activities.ask_questions, denial)

    def test_questions_found_are_kept_and_the_draft_waits_for_answers(self):
        denial, draft = agreed_draft()

        async def generate(denial_id):
            return QUESTIONS

        self.assertEqual(self.ask(denial, generate), 2)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.QUESTIONS)
        self.assertEqual(
            [q["label"] for q in draft.questions], [q for q, _ in QUESTIONS]
        )

    def test_questions_that_run_out_of_time_are_none_and_drafting_starts(self):
        denial, draft = agreed_draft()
        with patch.object(activities, "QUESTIONS_TIMEOUT_S", BOUND_S):
            self.assertEqual(self.ask(denial, asks_nothing_in_time), 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.DRAFTING)
        self.assertEqual(draft.questions, [])

    def test_a_draft_stopped_while_questions_are_made_stays_stopped(self):
        denial, draft = agreed_draft()

        async def stopped_meanwhile(denial_id):
            await sync_to_async(drafts.set_status)(draft, drafts.STOPPED)
            return QUESTIONS

        self.assertEqual(self.ask(denial, stopped_meanwhile), 0)
        self.assert_still_stopped(draft)
        self.assertEqual(draft.questions, [])
        self.assertFalse(self.run_activity(activities.start_drafting, denial))

    def test_a_stopped_draft_is_asked_nothing_and_stays_stopped(self):
        denial, draft = stopped_draft()
        generate = AsyncMock(return_value=QUESTIONS)
        self.assertEqual(self.ask(denial, generate), 0)
        generate.assert_not_called()
        self.assert_still_stopped(draft)
        self.assertEqual(draft.questions, [])


class StartDraftingTest(ActivityTestBase):
    def start(self, denial):
        return self.run_activity(activities.start_drafting, denial)

    def test_drafting_records_the_form_as_completed(self):
        denial, draft = agreed_draft()
        self.assertTrue(self.start(denial))
        self.assertTrue(
            IntakeJourneyEvent.objects.filter(
                denial=denial, event_type=intake_outbox.FORM_COMPLETED
            ).exists()
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.DRAFTING)

    def test_an_expired_draft_does_not_start_drafting(self):
        denial, draft = agreed_draft()
        AssistantDraft.objects.filter(pk=draft.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertFalse(self.start(denial))
        self.assertFalse(IntakeJourneyEvent.objects.filter(denial=denial).exists())
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.READING)

    def test_a_draft_stopped_after_its_check_does_not_start_drafting(self):
        # Agree stops the draft between this step's look and its write.
        denial, draft = stopped_draft()
        with patch.object(activities, "_stopped", return_value=False):
            self.assertFalse(self.start(denial))
        self.assertFalse(IntakeJourneyEvent.objects.filter(denial=denial).exists())
        self.assert_still_stopped(draft)

    def test_a_stopped_draft_does_not_start_drafting(self):
        denial, draft = stopped_draft()
        self.assertFalse(self.start(denial))
        self.assertFalse(IntakeJourneyEvent.objects.filter(denial=denial).exists())
        self.assert_still_stopped(draft)


class FinishDraftsTest(ActivityTestBase):
    def finish(self, denial):
        return self.run_activity(activities.finish_drafts, denial)

    def test_a_generation_the_site_holds_is_on_site_whatever_was_stored(self):
        denial, draft = agreed_draft()
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=LETTER)
        generation_lease.acquire(
            denial, generation_lease.new_holder("interactive"), steal=True
        )
        self.assertEqual(self.finish(denial), drafts.ON_SITE)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.ON_SITE)

    def test_a_background_generation_with_a_letter_is_ready(self):
        denial, draft = agreed_draft()
        generation_lease.acquire(denial, generation_lease.new_holder("journey"))
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=LETTER)
        self.assertEqual(self.finish(denial), drafts.READY)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.READY)

    def test_a_background_generation_with_no_letter_is_stopped(self):
        denial, draft = agreed_draft()
        generation_lease.acquire(denial, generation_lease.new_holder("journey"))
        self.assertEqual(self.finish(denial), drafts.STOPPED)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.STOPPED)

    def test_a_stopped_draft_stays_stopped_whatever_was_stored(self):
        denial, draft = stopped_draft()
        generation_lease.acquire(denial, generation_lease.new_holder("journey"))
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=LETTER)
        self.assertEqual(self.finish(denial), drafts.STOPPED)
        self.assert_still_stopped(draft)


class MarkDraftStatusTest(ActivityTestBase):
    def test_an_unknown_status_is_refused_for_good_and_changes_nothing(self):
        denial, draft = agreed_draft()
        with self.assertRaises(ApplicationError) as refused:
            self.run_activity(activities.mark_draft_status, denial, "made-up")
        self.assertTrue(refused.exception.non_retryable)
        draft.refresh_from_db()
        self.assertEqual(draft.status, drafts.READING)

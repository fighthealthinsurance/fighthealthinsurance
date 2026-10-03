"""Re-POSTing the scrub form reuses the denial the session already started.

The scrub form renders its next step directly instead of redirecting, so a
reload (or a back-then-resubmit, or a double click) re-POSTs it. Every such
POST used to INSERT a brand new Denial -- and with it a brand new multi-day
intake journey, a second speculative-appeal precompute, and a second set of
follow-up emails -- because the form carries no denial id and the session's
``denial_uuid`` was written but never read back.

These tests pin the reuse rule: the session's denial is updated in place, but
only for the same person, only while their journey is unfinished, and only
inside the recency window. Every other case must still create a new denial.
"""

import contextlib
import datetime
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync, sync_to_async
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper
from fighthealthinsurance.models import (
    Denial,
    DenialTypes,
    DenialTypesRelation,
    IntakeJourneyEvent,
)
from fighthealthinsurance.views import DENIAL_SESSION_REUSE_WINDOW


class DenialSessionReuseTest(TestCase):
    """One browser session mid-intake == one Denial row."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    EMAIL = "reuse@example.com"
    OTHER_EMAIL = "someone-else@example.com"

    def setUp(self):
        self.client = Client()

    def _post(self, email=None, denial_text="Your claim has been denied."):
        """Submit the consumer scrub form on the current session."""
        return self.client.post(
            reverse("process"),
            {
                "email": email or self.EMAIL,
                "denial_text": denial_text,
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
            follow=True,
        )

    def _first_submission(self) -> Denial:
        """The submission every test starts from, and the row it created."""
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Denial.objects.count(), 1)
        return Denial.objects.get()

    def test_resubmitting_the_form_does_not_create_a_second_denial(self):
        first = self._first_submission()

        self._post()

        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(Denial.objects.get().denial_id, first.denial_id)

    def test_a_reused_denial_keeps_its_uuid_so_the_journey_id_collides(self):
        """The stable uuid is the whole point: ``intake-{uuid}`` only collides
        (and Temporal's REJECT_DUPLICATE only fires) if it does not change."""
        first = self._first_submission()

        self._post()

        reused = Denial.objects.get()
        self.assertEqual(reused.uuid, first.uuid)
        self.assertEqual(self.client.session["denial_uuid"], str(first.uuid))

    def test_resubmitting_updates_the_reused_denial_in_place(self):
        first = self._first_submission()

        self._post(denial_text="Your claim has been denied. Corrected text.")

        reused = Denial.objects.get()
        self.assertEqual(reused.denial_id, first.denial_id)
        self.assertEqual(
            reused.denial_text, "Your claim has been denied. Corrected text."
        )

    def test_a_different_email_never_reuses_the_session_denial(self):
        """A stale or shared session must not hand one person's denial to
        somebody else, so a mismatched email always starts a new row."""
        first = self._first_submission()

        self._post(email=self.OTHER_EMAIL)

        self.assertEqual(Denial.objects.count(), 2)
        newest = Denial.objects.exclude(denial_id=first.denial_id).get()
        self.assertEqual(newest.hashed_email, Denial.get_hashed_email(self.OTHER_EMAIL))

    def test_a_completed_journey_starts_a_new_denial(self):
        """Once the intake journey finished, the next submission is a new case."""
        first = self._first_submission()
        IntakeJourneyEvent.objects.create(
            denial=first, event_type=IntakeJourneyEvent.FORM_COMPLETED
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 2)

    def test_an_unfinished_journey_still_reuses_the_denial(self):
        """Only FORM_COMPLETED blocks reuse -- the intake_started intent that
        every denial gets must not."""
        first = self._first_submission()
        IntakeJourneyEvent.objects.create(
            denial=first, event_type=IntakeJourneyEvent.INTAKE_STARTED
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(Denial.objects.get().denial_id, first.denial_id)

    def test_a_denial_older_than_the_window_is_not_reused(self):
        """Coming back to the same session much later is a different denial."""
        first = self._first_submission()
        Denial.objects.filter(denial_id=first.denial_id).update(
            created=timezone.now()
            - DENIAL_SESSION_REUSE_WINDOW
            - datetime.timedelta(minutes=1)
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 2)

    def test_a_denial_inside_the_window_is_still_reused(self):
        """The recency bound is a window, not an instant."""
        first = self._first_submission()
        Denial.objects.filter(denial_id=first.denial_id).update(
            created=timezone.now()
            - DENIAL_SESSION_REUSE_WINDOW
            + datetime.timedelta(minutes=5)
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(Denial.objects.get().denial_id, first.denial_id)

    def test_a_denial_with_no_creation_timestamp_is_not_reused(self):
        """``created`` is nullable on rows predating the column; a row whose
        age cannot be established is not treatable as recent."""
        first = self._first_submission()
        Denial.objects.filter(denial_id=first.denial_id).update(created=None)

        self._post()

        self.assertEqual(Denial.objects.count(), 2)

    def test_a_session_without_a_denial_uuid_creates_a_new_denial(self):
        """The unchanged path: no session state, no reuse."""
        self._first_submission()

        fresh_browser = Client()
        fresh_browser.post(
            reverse("process"),
            {
                "email": self.EMAIL,
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
            follow=True,
        )

        self.assertEqual(Denial.objects.count(), 2)


# The extractors other than the procedure/diagnosis reader and the denial-type
# match. Each one is its own model call; none of them bears on what these
# tests are about.
_OTHER_EXTRACTORS = (
    "extract_set_fax_number",
    "extract_set_insurance_company",
    "match_insurance_plan_from_regex",
    "extract_set_plan_id",
    "extract_set_claim_id",
    "extract_set_date_of_service",
    "extract_set_regulator",
    "extract_set_triage",
)


async def _swallow(coro, *args, **kwargs):
    """Stand in for fire_and_forget_in_new_threadpool without leaving coroutines."""
    close = getattr(coro, "close", None)
    if close is not None:
        close()
    return None


class ANewLetterOnTheReusedDenialTest(TestCase):
    """A different letter on the reused denial is read for itself.

    The reuse above keeps one row per person for a day, so a second, different
    letter lands on the row the first one filled in. What was read out of the
    first letter goes, the second is read afresh, and what the person typed
    stays theirs. The model and the denial-type match are stubbed and keyed on
    the letter they are handed; everything between the form and the row is
    the real code.
    """

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    EMAIL = "new-letter@example.com"
    LETTER_A = "We denied the knee MRI you asked about for your knee pain."
    LETTER_B = "We denied the insulin pump you asked about for type 1 diabetes."
    # What the model reads out of each letter: (procedure, diagnosis).
    READS = {
        LETTER_A: ("knee MRI", "knee pain"),
        LETTER_B: ("insulin pump", "type 1 diabetes"),
    }
    QUESTIONS = {
        LETTER_A: [["Did your doctor try an X-ray before the MRI?", ""]],
        LETTER_B: [["How often are you checking your blood sugar?", ""]],
    }
    CITATIONS = {
        LETTER_A: ["about the knee MRI"],
        LETTER_B: ["about the insulin pump"],
    }

    def setUp(self):
        self.client = Client()
        # regex_src caches the DataSource row on the class.
        DenialCreatorHelper._regex_src = None
        self.imaging = DenialTypes.objects.create(
            name="Imaging (test)", regex="MRI", negative_regex=""
        )
        self.equipment = DenialTypes.objects.create(
            name="Equipment (test)", regex="pump", negative_regex=""
        )
        self.types_in = {
            self.LETTER_A: [self.imaging],
            self.LETTER_B: [self.equipment],
        }

    def _submit(self, letter) -> Denial:
        """The scan page, on this browser session."""
        response = self.client.post(
            reverse("process"),
            {
                "email": self.EMAIL,
                "denial_text": letter,
                "pii": "on",
                "personalonly": "on",
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        return Denial.objects.get(uuid=self.client.session["denial_uuid"])

    def _replaced_by(self, letter):
        """The person submitting ``letter`` on the scan page, as a coroutine
        a stubbed model call can await while it is still running."""

        async def submit():
            await sync_to_async(self._submit)(letter)

        return submit

    def _read(self, denial, meanwhile=None, fails=False, retry=False) -> AsyncMock:
        """The extraction page: one run of extract_entity on the row.

        Returns the stubbed reader, so a test can tell whether the
        already-done gate let the letter be read. ``meanwhile`` runs inside
        the model call, before it answers; ``fails`` makes the model call
        raise; ``retry`` is the page's try-again button.
        """

        async def reads(denial_text):
            if meanwhile is not None:
                await meanwhile()
            if fails:
                raise RuntimeError("the model is down")
            return self.READS[denial_text]

        async def types_in(denial_text, **kwargs):
            return self.types_in[denial_text]

        reader = AsyncMock(side_effect=reads)
        with contextlib.ExitStack() as stack:
            for name in _OTHER_EXTRACTORS:
                stack.enter_context(
                    patch.object(
                        DenialCreatorHelper, name, new=AsyncMock(return_value=None)
                    )
                )
            stack.enter_context(
                patch(
                    "fighthealthinsurance.common_view_logic.appealGenerator."
                    "get_procedure_and_diagnosis",
                    new=reader,
                )
            )
            stack.enter_context(
                patch.object(
                    DenialCreatorHelper.regex_denial_processor,
                    "get_denialtype",
                    new=AsyncMock(side_effect=types_in),
                )
            )
            stack.enter_context(
                patch.object(
                    MLPlanDocHelper,
                    "generate_plan_documents_summary",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch.object(
                    DenialCreatorHelper,
                    "_maybe_dispatch_ucr",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch.object(
                    common_view_logic, "fire_and_forget_in_new_threadpool", _swallow
                )
            )

            async def run():
                return [
                    record
                    async for record in DenialCreatorHelper.extract_entity(
                        denial.denial_id, retry=retry
                    )
                ]

            async_to_sync(run)()
        return reader

    def _the_speculative_pass_asks(self, denial):
        """What build_speculative_context stores for the letter on the row.

        ``_read`` swallows the background work, so the candidate set it
        would have written is put there directly.
        """
        denial.refresh_from_db()
        Denial.objects.filter(denial_id=denial.denial_id).update(
            candidate_generated_questions=self.QUESTIONS[denial.denial_text]
        )

    def _ask(self, denial, meanwhile=None):
        """The questions step, with the model asking about the letter."""

        async def specific(denial_text, **kwargs):
            if meanwhile is not None:
                await meanwhile()
            return self.QUESTIONS[denial_text]

        with patch.object(
            MLAppealQuestionsHelper,
            "generate_generic_questions",
            new=AsyncMock(return_value=None),
        ), patch.object(
            MLAppealQuestionsHelper,
            "generate_specific_questions",
            new=AsyncMock(side_effect=specific),
        ), patch.object(
            common_view_logic, "fire_and_forget_in_new_threadpool", _swallow
        ):
            return async_to_sync(DenialCreatorHelper.generate_appeal_questions)(
                denial.denial_id
            )

    def _cite(self, denial, speculative, meanwhile=None):
        """The citation step on the row, with the model citing the letter."""

        async def cites(denial, **kwargs):
            if meanwhile is not None:
                await meanwhile()
            return self.CITATIONS[denial.denial_text]

        denial.refresh_from_db()
        with patch.object(
            MLCitationsHelper,
            "_generate_citations_for_denial",
            new=AsyncMock(side_effect=cites),
        ):
            return async_to_sync(MLCitationsHelper.generate_citations_for_denial)(
                denial, speculative=speculative
            )

    def _types_on(self, denial):
        return set(denial.denial_type.values_list("name", flat=True))

    def _first_letter_read_and_asked_about(self) -> Denial:
        """Letter A submitted, read, and its questions generated."""
        denial = self._submit(self.LETTER_A)
        self._read(denial)
        self._the_speculative_pass_asks(denial)
        self._ask(denial)
        denial.refresh_from_db()
        self.assertEqual(denial.procedure, "knee MRI")
        self.assertEqual(self._types_on(denial), {"Imaging (test)"})
        self.assertEqual(denial.generated_questions, self.QUESTIONS[self.LETTER_A])
        return denial

    def test_a_different_letter_is_read_for_its_own_details(self):
        first = self._first_letter_read_and_asked_about()

        denial = self._submit(self.LETTER_B)
        self._read(denial)
        self._ask(denial)

        denial.refresh_from_db()
        self.assertEqual(denial.denial_id, first.denial_id)
        self.assertEqual(
            (denial.procedure, denial.diagnosis), ("insulin pump", "type 1 diabetes")
        )
        self.assertEqual(self._types_on(denial), {"Equipment (test)"})
        self.assertEqual(denial.generated_questions, self.QUESTIONS[self.LETTER_B])

    def test_what_the_person_typed_survives_a_different_letter(self):
        denial = self._first_letter_read_and_asked_about()
        # Corrected on the review page.
        Denial.objects.filter(denial_id=denial.denial_id).update(
            procedure="knee MRI with contrast", diagnosis="torn meniscus"
        )

        denial = self._submit(self.LETTER_B)
        self._read(denial)

        denial.refresh_from_db()
        self.assertEqual(
            (denial.procedure, denial.diagnosis),
            ("knee MRI with contrast", "torn meniscus"),
        )

    def test_a_different_letter_is_asked_about_when_the_typed_details_hold(self):
        """The question set is stamped for the procedure and diagnosis, and
        typed ones survive the new letter, so the stamp alone would still
        call the first letter's questions current."""
        denial = self._first_letter_read_and_asked_about()
        Denial.objects.filter(denial_id=denial.denial_id).update(
            procedure="knee MRI with contrast", diagnosis="torn meniscus"
        )
        self._ask(denial)

        denial = self._submit(self.LETTER_B)
        self._read(denial)
        self._ask(denial)

        denial.refresh_from_db()
        self.assertEqual(denial.generated_questions, self.QUESTIONS[self.LETTER_B])

    def test_a_different_letter_is_cited_for_itself(self):
        denial = self._first_letter_read_and_asked_about()
        self._cite(denial, speculative=True)
        denial.refresh_from_db()
        self.assertEqual(
            denial.candidate_ml_citation_context, self.CITATIONS[self.LETTER_A]
        )

        denial = self._submit(self.LETTER_B)
        self._read(denial)
        # The speculative pass, then the questions step's citations, which
        # are the ones the appeal is written with.
        self._cite(denial, speculative=True)
        self._cite(denial, speculative=False)

        denial.refresh_from_db()
        self.assertEqual(denial.ml_citation_context, self.CITATIONS[self.LETTER_B])

    def test_a_type_the_person_added_survives_a_different_letter(self):
        denial = self._first_letter_read_and_asked_about()
        added = DenialTypes.objects.create(
            name="Out of network (test)", regex="never matches", negative_regex=""
        )
        # The review page's denial_type.set() writes the row with no source.
        DenialTypesRelation.objects.create(denial=denial, denial_type=added)

        denial = self._submit(self.LETTER_B)
        self._read(denial)

        self.assertEqual(
            self._types_on(denial), {"Out of network (test)", "Equipment (test)"}
        )

    def test_the_same_letter_again_changes_nothing(self):
        first = self._first_letter_read_and_asked_about()

        denial = self._submit(self.LETTER_A)
        reader = self._read(denial)

        denial.refresh_from_db()
        self.assertEqual(reader.await_count, 0, "the same letter was read twice")
        self.assertEqual(
            (denial.procedure, denial.diagnosis),
            (first.procedure, first.diagnosis),
        )
        self.assertEqual(denial.candidate_procedure, first.candidate_procedure)
        self.assertEqual(self._types_on(denial), {"Imaging (test)"})
        self.assertEqual(denial.generated_questions, first.generated_questions)
        self.assertEqual(denial.generated_questions_for, first.generated_questions_for)

    def test_a_cleanup_that_fails_part_way_leaves_the_row_as_it_was(self):
        """The extracted values, the detected types and the finished flag
        are cleared together, so a failure part way never leaves the values
        gone with the flag still saying the letter was read."""
        first = self._first_letter_read_and_asked_about()

        with patch.object(
            DenialTypesRelation.objects,
            "filter",
            side_effect=DatabaseError("the database went away"),
        ):
            denial = self._submit(self.LETTER_B)

        denial.refresh_from_db()
        self.assertEqual(denial.denial_text, self.LETTER_B)
        self.assertEqual(
            (denial.procedure, denial.diagnosis),
            (first.procedure, first.diagnosis),
        )
        self.assertEqual(denial.candidate_procedure, first.candidate_procedure)
        self.assertTrue(denial.extract_procedure_diagnosis_finished)
        self.assertEqual(self._types_on(denial), {"Imaging (test)"})

    def _ask_speculatively(self, denial, meanwhile=None):
        """The speculative pass's questions, on the copy of the row it holds."""

        async def specific(denial_text, **kwargs):
            if meanwhile is not None:
                await meanwhile()
            return self.QUESTIONS[denial_text]

        with patch.object(
            MLAppealQuestionsHelper,
            "generate_generic_questions",
            new=AsyncMock(return_value=None),
        ), patch.object(
            MLAppealQuestionsHelper,
            "generate_specific_questions",
            new=AsyncMock(side_effect=specific),
        ):
            return async_to_sync(MLAppealQuestionsHelper.generate_questions_for_denial)(
                denial, speculative=True
            )

    def test_a_different_letter_is_read_when_only_the_procedure_was_typed(self):
        denial = self._first_letter_read_and_asked_about()
        # Corrected on the review page; the diagnosis is still the one read
        # out of the first letter.
        Denial.objects.filter(denial_id=denial.denial_id).update(
            procedure="knee MRI with contrast"
        )

        denial = self._submit(self.LETTER_B)
        reader = self._read(denial)

        denial.refresh_from_db()
        self.assertEqual(reader.await_count, 1, "the new letter was not read")
        self.assertEqual(
            (denial.procedure, denial.diagnosis),
            ("knee MRI with contrast", "type 1 diabetes"),
        )
        self.assertEqual(
            (denial.candidate_procedure, denial.candidate_diagnosis),
            ("insulin pump", "type 1 diabetes"),
        )
        self.assertEqual(self._types_on(denial), {"Equipment (test)"})

    def test_a_different_letter_is_read_when_both_details_were_typed(self):
        """Nothing is left to fill in, and the letter is still read: the
        candidate copies and the detected types are the new letter's."""
        denial = self._first_letter_read_and_asked_about()
        Denial.objects.filter(denial_id=denial.denial_id).update(
            procedure="knee MRI with contrast", diagnosis="torn meniscus"
        )

        denial = self._submit(self.LETTER_B)
        reader = self._read(denial)

        denial.refresh_from_db()
        self.assertEqual(reader.await_count, 1, "the new letter was not read")
        self.assertEqual(
            (denial.procedure, denial.diagnosis),
            ("knee MRI with contrast", "torn meniscus"),
        )
        self.assertEqual(
            (denial.candidate_procedure, denial.candidate_diagnosis),
            ("insulin pump", "type 1 diabetes"),
        )
        self.assertEqual(self._types_on(denial), {"Equipment (test)"})

    def test_a_different_letter_is_read_again_after_a_retry_that_failed(self):
        """The try-again button spends an attempt; it does not mark the new
        letter read, so the next visit still reads it."""
        denial = self._first_letter_read_and_asked_about()
        Denial.objects.filter(denial_id=denial.denial_id).update(
            procedure="knee MRI with contrast"
        )
        denial = self._submit(self.LETTER_B)
        self._read(denial, fails=True)
        self._read(denial, fails=True, retry=True)

        reader = self._read(denial)

        denial.refresh_from_db()
        self.assertEqual(reader.await_count, 1, "the new letter was not read")
        self.assertEqual(denial.diagnosis, "type 1 diabetes")

    def test_citations_the_questions_step_kept_go_with_the_letter(self):
        denial = self._first_letter_read_and_asked_about()
        # The questions step's citations, which the appeal is written with.
        self._cite(denial, speculative=False)
        denial.refresh_from_db()
        self.assertEqual(denial.ml_citation_context, self.CITATIONS[self.LETTER_A])

        denial = self._submit(self.LETTER_B)
        self._read(denial)
        cited = self._cite(denial, speculative=False)

        denial.refresh_from_db()
        self.assertEqual(cited, self.CITATIONS[self.LETTER_B])
        self.assertEqual(denial.ml_citation_context, self.CITATIONS[self.LETTER_B])

    # The research the appeal step stores, each built from the procedure and
    # diagnosis read out of the letter, or from the letter's own codes.
    RESEARCH = {
        "pubmed_context": "studies on the knee MRI",
        "pubmed_ids_json": ["12345"],
        "nice_context": "NICE guidance on the knee MRI",
        "rag_context": "guidelines for the knee MRI codes",
        "imr_context": "past reviews of knee MRI denials",
    }

    def test_research_built_for_the_first_letter_goes_with_it(self):
        denial = self._first_letter_read_and_asked_about()
        Denial.objects.filter(denial_id=denial.denial_id).update(**self.RESEARCH)

        denial = self._submit(self.LETTER_B)

        denial.refresh_from_db()
        self.assertEqual(
            {column: getattr(denial, column) for column in self.RESEARCH},
            {column: None for column in self.RESEARCH},
        )

    def test_the_same_letter_again_keeps_its_citations_and_research(self):
        denial = self._first_letter_read_and_asked_about()
        self._cite(denial, speculative=False)
        Denial.objects.filter(denial_id=denial.denial_id).update(**self.RESEARCH)

        denial = self._submit(self.LETTER_A)

        denial.refresh_from_db()
        self.assertEqual(denial.ml_citation_context, self.CITATIONS[self.LETTER_A])
        self.assertEqual(
            {column: getattr(denial, column) for column in self.RESEARCH},
            self.RESEARCH,
        )

    # Work for the first letter that is still running when the second one is
    # submitted. Each step's model call submits letter B before it answers
    # about letter A, which is the order these land in when the person is
    # quicker than the model.

    def test_a_read_of_the_first_letter_that_finishes_late_is_dropped(self):
        denial = self._submit(self.LETTER_A)

        self._read(denial, meanwhile=self._replaced_by(self.LETTER_B))

        denial.refresh_from_db()
        self.assertEqual(denial.denial_text, self.LETTER_B)
        self.assertEqual((denial.procedure, denial.diagnosis), (None, None))
        self.assertEqual(
            (denial.candidate_procedure, denial.candidate_diagnosis), (None, None)
        )
        self.assertFalse(denial.extract_procedure_diagnosis_finished)
        reader = self._read(denial)
        denial.refresh_from_db()
        self.assertEqual(reader.await_count, 1, "the new letter was not read")
        self.assertEqual(
            (denial.procedure, denial.diagnosis), ("insulin pump", "type 1 diabetes")
        )

    def test_denial_types_matched_in_the_first_letter_that_land_late_are_dropped(
        self,
    ):
        denial = self._submit(self.LETTER_A)
        replace = self._replaced_by(self.LETTER_B)

        async def types_in(denial_text, **kwargs):
            await replace()
            return self.types_in[denial_text]

        with patch.object(
            DenialCreatorHelper.regex_denial_processor,
            "get_denialtype",
            new=AsyncMock(side_effect=types_in),
        ):
            async_to_sync(DenialCreatorHelper.extract_set_denialtype)(denial.denial_id)

        self.assertEqual(self._types_on(denial), set())

    def test_speculative_citations_for_the_first_letter_that_land_late_are_dropped(
        self,
    ):
        denial = self._first_letter_read_and_asked_about()

        self._cite(denial, speculative=True, meanwhile=self._replaced_by(self.LETTER_B))

        denial.refresh_from_db()
        self.assertIsNone(denial.candidate_ml_citation_context)
        self._read(denial)
        self.assertEqual(
            self._cite(denial, speculative=False), self.CITATIONS[self.LETTER_B]
        )

    def test_citations_for_the_first_letter_that_land_late_are_dropped(self):
        denial = self._first_letter_read_and_asked_about()

        self._cite(
            denial, speculative=False, meanwhile=self._replaced_by(self.LETTER_B)
        )

        denial.refresh_from_db()
        self.assertIsNone(denial.ml_citation_context)
        self._read(denial)
        self.assertEqual(
            self._cite(denial, speculative=False), self.CITATIONS[self.LETTER_B]
        )

    def test_speculative_questions_for_the_first_letter_that_land_late_are_dropped(
        self,
    ):
        denial = self._submit(self.LETTER_A)
        self._read(denial)
        denial.refresh_from_db()

        self._ask_speculatively(denial, meanwhile=self._replaced_by(self.LETTER_B))

        denial.refresh_from_db()
        self.assertIsNone(denial.candidate_generated_questions)

    def test_questions_for_the_first_letter_that_land_late_are_dropped(self):
        """Typed details survive the new letter, so the question set's stamp
        alone would still call a set for the first letter current."""
        denial = self._submit(self.LETTER_A)
        self._read(denial)
        Denial.objects.filter(denial_id=denial.denial_id).update(
            procedure="knee MRI with contrast", diagnosis="torn meniscus"
        )

        self._ask(denial, meanwhile=self._replaced_by(self.LETTER_B))

        denial.refresh_from_db()
        self.assertIsNone(denial.generated_questions)
        self._read(denial)
        self.assertEqual(self._ask(denial), self.QUESTIONS[self.LETTER_B])


class ResearchForAReplacedLetterTest(TestCase):
    """A research lookup that started on the first letter and finishes after
    the second one replaced it stores nothing.

    The lookup is handed the copy of the row it started from, as the appeal
    step hands it one; the row has the new letter by the time it answers.
    """

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("research@example.com"),
            denial_text="We denied the knee MRI you asked about.",
            procedure="knee MRI",
            diagnosis="knee pain",
        )
        self.started_on = Denial.objects.get(denial_id=denial.denial_id)
        Denial.objects.filter(denial_id=denial.denial_id).update(
            denial_text="We denied the insulin pump you asked about."
        )
        self.denial = denial

    def _row(self) -> Denial:
        return Denial.objects.get(denial_id=self.denial.denial_id)

    def test_pubmed_context_for_a_replaced_letter_is_not_stored(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        with patch.object(
            PubMedTools,
            "_find_context_for_denial",
            new=AsyncMock(return_value="studies on the knee MRI"),
        ):
            async_to_sync(PubMedTools().find_context_for_denial)(self.started_on)

        self.assertIsNone(self._row().pubmed_context)

    def test_pubmed_articles_for_a_replaced_letter_are_not_stored(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        self.started_on.pubmed_ids_json = ["12345"]
        with patch.object(PubMedTools, "get_articles", new=AsyncMock(return_value=[])):
            async_to_sync(PubMedTools()._find_context_for_denial)(self.started_on)

        self.assertIsNone(self._row().pubmed_ids_json)

    def test_nice_context_for_a_replaced_letter_is_not_stored(self):
        from fighthealthinsurance.nice_tools import NICETools

        with patch.object(
            NICETools,
            "_find_context_for_denial",
            new=AsyncMock(return_value="NICE guidance on the knee MRI"),
        ):
            async_to_sync(NICETools(api_key="not-a-real-key").find_context_for_denial)(
                self.started_on
            )

        self.assertIsNone(self._row().nice_context)

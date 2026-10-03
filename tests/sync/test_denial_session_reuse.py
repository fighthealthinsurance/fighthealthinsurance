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

from asgiref.sync import async_to_sync
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
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
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        return Denial.objects.get(uuid=self.client.session["denial_uuid"])

    def _read(self, denial) -> AsyncMock:
        """The extraction page: one run of extract_entity on the row.

        Returns the stubbed reader, so a test can tell whether the
        already-done gate let the letter be read.
        """

        async def reads(denial_text):
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
                        denial.denial_id
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

    def _ask(self, denial):
        """The questions step, with the model asking about the letter."""

        async def specific(denial_text, **kwargs):
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

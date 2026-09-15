"""The questions step keeps the answers it was given.

Pinned regressions:

- answers were written into ``qa_context`` under the QUESTION TEXT while the
  fields carrying them were named ``appeal_generated_question_<n>``, so the
  rebuild looked the answers up by field name and never found them: pressing
  Back showed empty boxes and submitting again kept only what was retyped;
- ``find_next_steps_for_denial`` (the whole back-navigation path) built the
  page from a bare ``{}`` on top of that;
- a checked box posts ``"on"`` while the rebuild compared against ``"True"``,
  and an unticked box posts nothing at all, so ticking a box was forgotten
  and unticking one never took;
- the generated fields set the question as BOTH label and help_text, so
  ``as_table`` printed the same sentence above and below every box;
- the template branched on ``{% if combined %}``, a Django Form, which is
  truthy with no fields in it, so a run that produced nothing showed the
  "A few more questions" heading over an empty table;
- ``magic_combined_form`` merged a field name two denial types share with
  ``initial += initial``, which raised when the kept field had no default,
  and ``find_next_steps`` recovered from that raise by rebuilding the page
  with an empty answers dict -- wiping every answer the person had given;
- a denial type's ``appeal_text`` was passed in as
  ``initial={"medical_reason": ...}``. That is canned appeal boilerplate,
  not the person's words. See ``NoDenialTypeBoilerplateInTheAnswerBoxTest``;
- an answer to a generated question whose text collides with a key the
  review step owns overwrote that key. See ``ReservedQaKeyTest``.
"""

import json
import re
from unittest.mock import AsyncMock, patch

from django import forms as django_forms
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.common_view_logic import (
    QUESTIONS_OUTCOME_NONE,
    QUESTIONS_OUTCOME_PRESENT,
    QUESTIONS_OUTCOME_UNFINISHED,
    FindNextStepsHelper,
)
from fighthealthinsurance.denial_context import (
    qa_key_for_question,
    GENERATED_QUESTION_PREFIX,
    RESERVED_QA_KEYS,
    load_qa,
    question_field_name,
)
from fighthealthinsurance.form_utils import magic_combined_form
from fighthealthinsurance.forms import questions as question_forms
from fighthealthinsurance.models import Denial, DenialTypes

_QUESTIONS = (
    "fighthealthinsurance.common_view_logic.DenialCreatorHelper."
    "generate_appeal_questions"
)
_ML_HELPER = (
    "fighthealthinsurance.common_view_logic.MLAppealQuestionsHelper."
    "generate_questions_for_denial"
)

_Q1 = "How long have you been on this treatment?"
_Q2 = "What happened when you stopped it?"


class QuestionsStepTestBase(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.email = "questions@example.com"
        self.denial = Denial.objects.create(
            denial_id=7101,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(self.email),
            denial_text="Denied physical therapy.",
            procedure="physical therapy",
            diagnosis="chronic back pain",
            generated_questions=[[_Q1, ""], [_Q2, ""]],
        )

    def _ref(self):
        return {
            "denial_id": str(self.denial.denial_id),
            "email": self.email,
            "semi_sekret": "sekret",
        }

    def _generate_appeal(self, **answers):
        payload = self._ref()
        payload.update(answers)
        return self.client.post(reverse("generate_appeal"), payload)

    def _qa(self):
        self.denial.refresh_from_db()
        return json.loads(self.denial.qa_context)

    def denial_after_post(self):
        self.denial.refresh_from_db()
        return self.denial

    def _rebuild(self):
        self.denial.refresh_from_db()
        return FindNextStepsHelper.find_next_steps_for_denial(self.denial, self.email)


class GeneratedQuestionRoundTripTest(QuestionsStepTestBase):
    def test_post_writes_the_key_the_rebuild_reads(self):
        """The key the POST writes is the key the rebuild reads."""
        field_one = question_field_name(_Q1)
        field_two = question_field_name(_Q2)

        response = self._generate_appeal(
            **{field_one: "About four months", field_two: "The pain came back"}
        )
        self.assertEqual(response.status_code, 200)

        stored = self._qa()
        self.assertEqual(stored[_Q1], "About four months")
        self.assertEqual(stored[_Q2], "The pain came back")

        combined = self._rebuild().combined_form
        self.assertEqual(combined.fields[field_one].initial, "About four months")
        self.assertEqual(combined.fields[field_two].initial, "The pain came back")

    def test_the_medical_context_string_carries_the_question_not_an_identifier(self):
        """qa_context stays prose: generate_appeal formats it into the prompt.

        ``generate_appeal.py`` builds the medical context as
        ``"\\n".join(f"{k}:{v}" for k, v in json.loads(denial.qa_context).items())``.
        This reproduces that line over what the POST actually stored.
        """
        self._generate_appeal(**{question_field_name(_Q1): "About four months"})

        medical_context = "\n".join(f"{k}:{v}" for k, v in self._qa().items())
        self.assertIn(_Q1, medical_context)
        self.assertIn("About four months", medical_context)
        self.assertNotIn(GENERATED_QUESTION_PREFIX, medical_context)

    def test_an_answer_survives_the_questions_being_reordered(self):
        """The field identity is the question, not its position in the list."""
        self._generate_appeal(**{question_field_name(_Q1): "About four months"})

        self.denial.refresh_from_db()
        self.denial.generated_questions = [[_Q2, ""], [_Q1, ""]]
        self.denial.save(update_fields=["generated_questions"])

        combined = self._rebuild().combined_form
        self.assertEqual(
            combined.fields[question_field_name(_Q1)].initial, "About four months"
        )

    def test_a_positional_key_from_an_older_page_still_maps(self):
        """A tab rendered before this change posts appeal_generated_question_1."""
        response = self._generate_appeal(
            **{f"{GENERATED_QUESTION_PREFIX}2": "The pain came back"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._qa()[_Q2], "The pain came back")

    def test_build_question_forms_reads_existing_answers(self):
        """existing_answers is read, not merely accepted."""
        forms = FindNextStepsHelper._build_question_forms(
            self.denial, {_Q1: "seeded from the row"}
        )
        rendered = "".join(str(form) for form in forms)
        self.assertIn("seeded from the row", rendered)

    def test_the_get_path_loads_the_answers_from_the_row(self):
        """Back navigation rebuilds from the row, not from an empty dict."""
        self.denial.qa_context = json.dumps({_Q1: "answered before pressing back"})
        self.denial.save(update_fields=["qa_context"])

        response = self.client.get(reverse("find_next_steps"), self._ref())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "answered before pressing back")

    def test_no_question_text_appears_twice_on_the_page(self):
        """Label only. Label plus help_text printed the sentence twice."""
        self.denial.generated_questions = [[_Q1, ""], [_Q1, ""], [_Q2, ""]]
        self.denial.save(update_fields=["generated_questions"])

        response = self.client.get(reverse("find_next_steps"), self._ref())

        body = response.content.decode()
        self.assertEqual(body.count(_Q1), 1)
        self.assertEqual(body.count(_Q2), 1)


class ReservedQaKeyTest(QuestionsStepTestBase):
    """The review step writes four non-question keys into the same dict.

    This denial has no denial types, so every field on its questions page
    comes from generated_questions and nothing else.
    """

    _OLD_SHAPE = {
        _Q1: "eight weeks",
        _Q2: "it got worse",
        "denial date": "2024-03-02",
        "date of service": "2024-02-14",
        "date_of_service": "2024-02-14",
        "in_network": "False",
    }

    def setUp(self):
        super().setUp()
        self.denial.qa_context = json.dumps(self._OLD_SHAPE)
        self.denial.save(update_fields=["qa_context"])

    def test_the_four_reserved_keys_are_the_ones_the_review_post_writes(self):
        """find_next_steps writes exactly these into qa_context."""
        self.assertEqual(
            RESERVED_QA_KEYS,
            frozenset(
                {"denial date", "date of service", "date_of_service", "in_network"}
            ),
        )

    def test_an_old_shape_qa_context_still_rebuilds(self):
        """Text keys and nothing else: the shape an in-flight denial holds."""
        combined = self._rebuild().combined_form
        self.assertEqual(
            combined.fields[question_field_name(_Q1)].initial, "eight weeks"
        )
        self.assertEqual(
            combined.fields[question_field_name(_Q2)].initial, "it got worse"
        )

    def test_no_reserved_key_becomes_a_question_or_answers_one(self):
        """Even a generated question whose text IS a reserved key."""
        self.denial.generated_questions = [[_Q1, ""], ["date of service", ""]]
        self.denial.save(update_fields=["generated_questions"])

        combined = self._rebuild().combined_form

        # The page asks exactly the two generated questions, no reserved key.
        self.assertEqual(
            set(combined.fields),
            {question_field_name(_Q1), question_field_name("date of service")},
        )
        # And the one that collides with a reserved key is not answered from
        # the date the review step stored under it.
        self.assertEqual(
            combined.fields[question_field_name("date of service")].initial, ""
        )

    def test_the_reserved_keys_survive_the_next_post(self):
        self._generate_appeal(**{question_field_name(_Q1): "nine weeks"})

        stored = self._qa()
        self.assertEqual(stored[_Q1], "nine weeks")
        for reserved in RESERVED_QA_KEYS:
            self.assertEqual(stored[reserved], self._OLD_SHAPE[reserved])

    def test_answering_a_question_named_like_a_reserved_key_does_not_take_it(self):
        """The collision on the WRITE path, which read-side protection alone
        never reached: the answer landed straight on the reserved key,
        overwriting the review step's date, and Back then hid what had
        happened by refusing to read that key back."""
        self.denial.generated_questions = [[_Q1, ""], ["date of service", ""]]
        self.denial.save(update_fields=["generated_questions"])
        field = question_field_name("date of service")

        self._generate_appeal(**{field: "the follow-up on 2024-06-01"})

        stored = self._qa()
        self.assertEqual(stored["date of service"], self._OLD_SHAPE["date of service"])
        self.assertEqual(stored[field], "the follow-up on 2024-06-01")

    def test_that_answer_is_still_shown_back_to_the_person(self):
        """Filed out of the reserved namespace, it still has to come back."""
        self.denial.generated_questions = [[_Q1, ""], ["date of service", ""]]
        self.denial.save(update_fields=["generated_questions"])
        field = question_field_name("date of service")

        self._generate_appeal(**{field: "the follow-up on 2024-06-01"})

        combined = self._rebuild().combined_form
        self.assertEqual(combined.fields[field].initial, "the follow-up on 2024-06-01")


class WithdrawingAnAnswerTest(QuestionsStepTestBase):
    """An answer the person clears has to leave, and so has the sentence the
    answers derive for the prompt."""

    def setUp(self):
        super().setUp()
        self.denial.generated_questions = [[_Q1, ""]]
        self.denial.save(update_fields=["generated_questions"])
        self.field = question_field_name(_Q1)
        # Answers file under the question, not the field.
        self.key = qa_key_for_question(_Q1)

    def test_clearing_a_text_answer_removes_it(self):
        self._generate_appeal(**{self.field: "about six months"})
        self.assertEqual(self._qa()[self.key], "about six months")

        self._generate_appeal(**{self.field: ""})

        self.assertNotIn(self.key, self._qa())

    def test_a_blank_for_a_question_never_answered_writes_nothing(self):
        self._generate_appeal(**{self.field: ""})

        after = self.denial_after_post().qa_context
        self.assertTrue(not after or self.key not in json.loads(after), after)

    def test_the_derived_medical_context_is_withdrawn_when_the_answers_stop_deriving_it(
        self,
    ):
        from fighthealthinsurance.common_view_logic import (
            record_derived_medical_context,
        )

        self.denial.qa_context = json.dumps(
            {"medical_context": "This is an urgent claim.", "kept": "yes"}
        )

        changed = record_derived_medical_context(self.denial, set())

        self.assertTrue(changed)
        self.assertEqual(json.loads(self.denial.qa_context), {"kept": "yes"})

    def test_the_derived_medical_context_is_replaced_not_added_to(self):
        from fighthealthinsurance.common_view_logic import (
            record_derived_medical_context,
        )

        self.denial.qa_context = json.dumps(
            {"medical_context": "This is an urgent claim."}
        )

        record_derived_medical_context(self.denial, {"The patient is pregnant."})

        self.assertEqual(
            json.loads(self.denial.qa_context)["medical_context"],
            "The patient is pregnant.",
        )


class WithdrawalEdgesTest(QuestionsStepTestBase):
    def setUp(self):
        super().setUp()
        self.denial.generated_questions = [[_Q1, ""]]
        self.denial.save(update_fields=["generated_questions"])
        self.field = question_field_name(_Q1)
        self.key = qa_key_for_question(_Q1)

    def test_a_form_deriving_an_empty_sentence_withdraws_the_old_one(self):
        """InsuranceQuestions.medical_context() returns "" when every box is
        unticked, so the collector hands over {""}: not a sentence."""
        from fighthealthinsurance.common_view_logic import (
            record_derived_medical_context,
        )

        self.denial.qa_context = json.dumps(
            {"medical_context": "This is an urgent claim."}
        )

        record_derived_medical_context(self.denial, {""})

        self.assertNotIn("medical_context", json.loads(self.denial.qa_context))

    def test_an_answer_filed_under_the_raw_field_name_is_withdrawn_with_the_question(
        self,
    ):
        """A submission while the question was unmappable filed the answer
        under the raw field name. Clearing the question now has to retire
        that copy too, or Back shows it again."""
        self.denial.qa_context = json.dumps({self.field: "an older answer"})
        self.denial.save(update_fields=["qa_context"])

        self._generate_appeal(**{self.field: ""})

        self.assertNotIn(self.field, self._qa())

    def test_a_suggested_answer_with_markup_reaches_the_page_as_text(self):
        self.denial.generated_questions = [
            [_Q1, "<b onmouseover=alert(1)>six</b> months"]
        ]
        self.denial.save(update_fields=["generated_questions"])

        response = self.client.get(reverse("find_next_steps"), self._ref())

        body = response.content.decode()
        self.assertNotIn("<b onmouseover", body)
        self.assertIn("&lt;b onmouseover", body)


class QuestionsAreForTheInputsTheyWereGeneratedForTest(QuestionsStepTestBase):
    """The stored set carries what it was generated for, and the claim
    compares that with the row under the lock."""

    def setUp(self):
        super().setUp()
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            _claim_generated_questions_sync,
            questions_fingerprint,
        )

        self.claim = _claim_generated_questions_sync
        self.fp = questions_fingerprint
        self.denial.generated_questions = None
        self.denial.generated_questions_for = None
        self.denial.procedure = "CT scan"
        self.denial.diagnosis = "knee pain"
        self.denial.save(
            update_fields=[
                "generated_questions",
                "generated_questions_for",
                "procedure",
                "diagnosis",
            ]
        )

    def test_a_run_for_inputs_since_corrected_stores_nothing(self):
        """Started for an MRI, finished after the person corrected it to CT."""
        stale = self.claim(
            self.denial.denial_id, [["Q for MRI", ""]], self.fp("MRI", "knee pain")
        )

        self.denial.refresh_from_db()
        self.assertIsNone(stale)
        self.assertIsNone(self.denial.generated_questions)

    def test_the_first_set_for_the_current_inputs_keeps_the_slot(self):
        first = self.claim(
            self.denial.denial_id, [["Q one", ""]], self.fp("CT scan", "knee pain")
        )
        second = self.claim(
            self.denial.denial_id, [["Q two", ""]], self.fp("CT scan", "knee pain")
        )

        self.assertEqual(first, [["Q one", ""]])
        self.assertEqual(second, [["Q one", ""]], "answers are filed against the first")

    def test_a_set_for_the_current_inputs_replaces_one_for_old_inputs(self):
        self.denial.generated_questions = [["Q for MRI", ""]]
        self.denial.generated_questions_for = self.fp("MRI", "knee pain")
        self.denial.save(
            update_fields=["generated_questions", "generated_questions_for"]
        )

        stored = self.claim(
            self.denial.denial_id, [["Q for CT", ""]], self.fp("CT scan", "knee pain")
        )

        self.assertEqual(stored, [["Q for CT", ""]])

    def test_a_finished_run_with_nothing_to_ask_is_stored_as_empty(self):
        stored = self.claim(self.denial.denial_id, [], self.fp("CT scan", "knee pain"))

        self.denial.refresh_from_db()
        self.assertEqual(stored, [])
        self.assertEqual(self.denial.generated_questions, [])

    def test_a_set_from_before_the_stamp_is_not_regenerated_on_every_post(self):
        """A legacy row holds questions and no stamp. Regenerating it on each
        review POST would run the models every time and never stamp it."""
        from unittest.mock import AsyncMock, patch

        from fighthealthinsurance.common_view_logic import DenialCreatorHelper

        self.denial.generated_questions = [["Q from before the stamp", ""]]
        self.denial.generated_questions_for = None
        self.denial.save(
            update_fields=["generated_questions", "generated_questions_for"]
        )
        with patch.object(
            DenialCreatorHelper,
            "generate_appeal_questions",
            new=AsyncMock(return_value=[]),
        ) as generate:
            FindNextStepsHelper.find_next_steps(
                denial_id=self.denial.denial_id,
                email=self.email,
                semi_sekret=self.denial.semi_sekret,
                procedure="CT scan",
                diagnosis="knee pain",
                insurance_company="evilco",
                plan_id="1",
                claim_id="7",
                denial_type=None,
                denial_date=None,
            )
        generate.assert_not_awaited()

    def test_the_review_post_regenerates_for_corrected_inputs_and_not_otherwise(self):
        from unittest.mock import AsyncMock, patch

        from fighthealthinsurance.common_view_logic import DenialCreatorHelper

        self.denial.generated_questions = [["Q for MRI", ""]]
        self.denial.generated_questions_for = self.fp("MRI", "knee pain")
        self.denial.save(
            update_fields=["generated_questions", "generated_questions_for"]
        )
        with patch.object(
            DenialCreatorHelper,
            "generate_appeal_questions",
            new=AsyncMock(return_value=[]),
        ) as generate:
            FindNextStepsHelper.find_next_steps(
                denial_id=self.denial.denial_id,
                email=self.email,
                semi_sekret=self.denial.semi_sekret,
                procedure="CT scan",
                diagnosis="knee pain",
                insurance_company="evilco",
                plan_id="1",
                claim_id="7",
                denial_type=None,
                denial_date=None,
            )
        generate.assert_awaited_once()

        self.denial.generated_questions_for = self.fp("CT scan", "knee pain")
        self.denial.save(update_fields=["generated_questions_for"])
        with patch.object(
            DenialCreatorHelper,
            "generate_appeal_questions",
            new=AsyncMock(return_value=[]),
        ) as generate:
            FindNextStepsHelper.find_next_steps(
                denial_id=self.denial.denial_id,
                email=self.email,
                semi_sekret=self.denial.semi_sekret,
                procedure="CT scan",
                diagnosis="knee pain",
                insurance_company="evilco",
                plan_id="1",
                claim_id="7",
                denial_type=None,
                denial_date=None,
            )
        generate.assert_not_awaited()


class OnlyAQuestionnaireCanWithdrawTest(QuestionsStepTestBase):
    def test_generation_without_a_questionnaire_keeps_the_derived_sentence(self):
        from fighthealthinsurance.common_view_logic import (
            record_derived_medical_context,
        )

        self.denial.qa_context = json.dumps(
            {"medical_context": "This is an urgent claim."}
        )

        changed = record_derived_medical_context(self.denial, set(), withdraw=False)

        self.assertFalse(changed)
        self.assertEqual(
            json.loads(self.denial.qa_context)["medical_context"],
            "This is an urgent claim.",
        )

    def test_the_questionnaire_form_carries_its_mark_and_the_mark_is_not_stored(self):
        response = self.client.get(reverse("find_next_steps"), self._ref())
        self.assertIn('name="questionnaire"', response.content.decode())

        self._generate_appeal(questionnaire="1")

        after = self.denial_after_post().qa_context
        self.assertTrue(not after or "questionnaire" not in json.loads(after), after)


class AStaleSetIsNotRenderedTest(QuestionsStepTestBase):
    def test_a_set_for_a_corrected_procedure_is_not_rendered(self):
        """Regeneration failed and the MRI set is still on the row. The page
        must not ask MRI questions about a CT scan."""
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            questions_fingerprint,
        )

        self.denial.procedure = "CT scan"
        self.denial.diagnosis = "knee pain"
        self.denial.generated_questions = [["What about the MRI?", ""]]
        self.denial.generated_questions_for = questions_fingerprint("MRI", "knee pain")
        self.denial.save(
            update_fields=[
                "procedure",
                "diagnosis",
                "generated_questions",
                "generated_questions_for",
            ]
        )

        combined = self._rebuild().combined_form

        self.assertNotIn(question_field_name("What about the MRI?"), combined.fields)


class BackAfterAnUnfinishedRunTest(QuestionsStepTestBase):
    def test_back_after_a_run_that_never_finished_does_not_say_we_have_what_we_need(
        self,
    ):
        """Nothing stored means no run finished; Back used to call that
        finished and empty."""
        self.denial.generated_questions = None
        self.denial.save(update_fields=["generated_questions"])

        info = FindNextStepsHelper.find_next_steps_for_denial(self.denial, self.email)

        self.assertEqual(info.questions_outcome, "generation_unfinished")

    def test_back_after_a_finished_empty_run_says_no_questions(self):
        self.denial.generated_questions = []
        self.denial.save(update_fields=["generated_questions"])

        info = FindNextStepsHelper.find_next_steps_for_denial(self.denial, self.email)

        self.assertEqual(info.questions_outcome, "no_questions")


class InNetworkOwnershipTest(QuestionsStepTestBase):
    """Whoever can see the box owns the answer in it.

    ``in_network`` is the one key that is both something the review step can
    write and a checkbox on this page, and ``InsuranceQuestions.__init__``
    draws the line: it REMOVES the field whenever the review step owns it
    (``prof_pov``). Excluding ``in_network`` by name instead left every
    patient, who does see the box, unable to untick it.
    """

    def setUp(self):
        super().setUp()
        self.denial.generated_questions = []
        self.denial.save(update_fields=["generated_questions"])
        self.denial.denial_type.set(DenialTypes.objects.filter(pk=2))

    def test_in_network_is_rendered_as_a_checkbox_for_a_patient(self):
        combined = self._rebuild().combined_form
        self.assertIsInstance(combined.fields["in_network"], django_forms.BooleanField)

    def test_a_patient_can_untick_the_box_they_ticked(self):
        self._generate_appeal(medical_reason="my surgeon says so", in_network="on")
        self.assertEqual(self._qa()["in_network"], "on")

        self._generate_appeal(medical_reason="my surgeon says so")

        self.assertEqual(self._qa()["in_network"], "False")
        combined = self._rebuild().combined_form
        self.assertIs(combined.fields["in_network"].initial, False)

    def test_the_review_steps_answer_survives_when_it_is_not_on_this_page(self):
        """The professional was asked in the earlier form, so the field is
        not rendered here and nothing on this page can touch it."""
        self.denial.professional_to_finish = True
        self.denial.qa_context = json.dumps({"in_network": "True"})
        self.denial.save(update_fields=["professional_to_finish", "qa_context"])

        combined = self._rebuild().combined_form
        self.assertNotIn("in_network", combined.fields)

        self._generate_appeal(medical_reason="my surgeon says so")

        self.assertEqual(self._qa()["in_network"], "True")

    def test_the_date_keys_are_still_never_taken_for_an_answer(self):
        """The other reserved keys are dates the review step writes. They
        are not fields on this page, so no checkbox can reach them."""
        self.denial.qa_context = json.dumps(
            {"denial date": "2026-01-02", "date of service": "2026-01-01"}
        )
        self.denial.save(update_fields=["qa_context"])

        combined = self._rebuild().combined_form
        for reserved in RESERVED_QA_KEYS - {"in_network"}:
            self.assertNotIn(reserved, combined.fields)

        self._generate_appeal(medical_reason="my surgeon says so")

        self.assertEqual(self._qa()["denial date"], "2026-01-02")
        self.assertEqual(self._qa()["date of service"], "2026-01-01")


class CheckboxAnswerTest(QuestionsStepTestBase):
    """A checkbox posts "on" when ticked and nothing at all when not."""

    def setUp(self):
        super().setUp()
        self.denial.generated_questions = []
        self.denial.save(update_fields=["generated_questions"])
        self.denial.denial_type.set(DenialTypes.objects.filter(pk=3))

    def test_a_ticked_checkbox_comes_back_ticked(self):
        self._generate_appeal(emergency="on")

        self.assertEqual(self._qa()["emergency"], "on")
        combined = self._rebuild().combined_form
        self.assertIs(combined.fields["emergency"].initial, True)

    def test_a_checkbox_answered_and_then_unanswered_persists_as_false(self):
        self._generate_appeal(emergency="on")
        self._generate_appeal(prior_auth_id="PA-1")

        self.assertEqual(self._qa()["emergency"], "False")
        combined = self._rebuild().combined_form
        self.assertIs(combined.fields["emergency"].initial, False)

    def test_a_box_never_ticked_adds_no_false_to_the_prompt(self):
        """qa_context is read as prose, so "emergency:False" is noise.

        The second POST is the one under test: by then the denial has
        answers stored.
        """
        self._generate_appeal(prior_auth_id="PA-1")
        self._generate_appeal(prior_auth_id="PA-1", prior_auth_obtained="on")

        stored = self._qa()
        self.assertEqual(stored["prior_auth_obtained"], "on")
        self.assertNotIn("emergency", stored)
        self.assertNotIn("contact_insurance_before", stored)


class DenialTypeQuestionTest(QuestionsStepTestBase):
    def setUp(self):
        super().setUp()
        self.denial.generated_questions = []
        self.denial.save(update_fields=["generated_questions"])
        DenialTypes.objects.filter(pk=2).update(
            appeal_text="The treatment is medically necessary."
        )
        DenialTypes.objects.filter(pk=4).update(
            appeal_text="The treatment is not experimental."
        )

    def test_post_writes_the_key_the_rebuild_reads(self):
        self.denial.denial_type.set(DenialTypes.objects.filter(pk=2))

        self._generate_appeal(medical_reason="my surgeon says so")

        self.assertEqual(self._qa()["medical_reason"], "my surgeon says so")
        combined = self._rebuild().combined_form
        self.assertEqual(
            combined.fields["medical_reason"].initial, "my surgeon says so"
        )

    def test_the_denial_types_appeal_text_is_not_typed_into_the_answer_box(self):
        """``appeal_text`` is the canned paragraph the appeal builder falls
        back to, not a sentence the person wrote. It must never arrive in
        their answer box."""
        self.denial.denial_type.set(DenialTypes.objects.filter(pk=2))

        combined = self._rebuild().combined_form

        self.assertIn(combined.fields["medical_reason"].initial, (None, ""))

    def test_a_name_clash_between_two_denial_types_keeps_the_questionnaire(self):
        """Two types declaring medical_reason must not discard the page."""
        self.denial.denial_type.set(DenialTypes.objects.filter(pk__in=[2, 4]))
        self.denial.qa_context = json.dumps({"age": "41"})
        self.denial.save(update_fields=["qa_context"])

        combined = self._rebuild().combined_form

        # Every field from both forms is present...
        self.assertIn("medical_reason", combined.fields)
        self.assertIn("age", combined.fields)
        # ...neither type put its canned appeal paragraph in the box...
        self.assertIn(combined.fields["medical_reason"].initial, (None, ""))
        # ...and the answers already given survived.
        self.assertEqual(combined.fields["age"].initial, "41")


class NoDenialTypeBoilerplateInTheAnswerBoxTest(QuestionsStepTestBase):
    """No denial type's canned appeal paragraph reaches an answer box.

    Narrow on purpose. A GENERATED question still renders the model's
    suggested answer as its default, which is words the person did not
    write; that is the existing behaviour of the questions step and is not
    what this class is about. ``appeal_text`` is different: it is the
    paragraph the appeal builder falls back to when a form does NOT
    validate, several of them are longer than the field they would land in,
    and once it is in the box it goes into the model's medical context and
    the regulator letter as the patient's own account.
    """

    _ACA_PREFIX = "The ACA (and equivalent regulations for many non-ACA plans)"

    def setUp(self):
        super().setUp()
        self.denial.generated_questions = []
        self.denial.save(update_fields=["generated_questions"])

    def _preventive_care(self):
        denial_type = DenialTypes.objects.get(name="Preventive Care")
        self.denial.denial_type.set([denial_type])
        return denial_type

    def test_preventive_care_does_not_pre_type_the_aca_boilerplate(self):
        denial_type = self._preventive_care()
        # The fixture value this used to copy into the box, unchanged.
        self.assertTrue(denial_type.appeal_text.startswith(self._ACA_PREFIX))
        self.assertGreater(len(denial_type.appeal_text), 300)

        combined = self._rebuild().combined_form

        self.assertIn(combined.fields["medical_reason"].initial, (None, ""))
        self.assertNotIn(self._ACA_PREFIX, str(combined.as_table()))

    def test_a_preventive_care_page_submitted_unedited_still_validates(self):
        """The appeal builder runs ``form(parameters).is_valid()`` with no
        guard: an invalid form loses its preface, footer and every checkbox
        answer, and the letter falls back to the canned paragraph."""
        self._preventive_care()
        combined = self._rebuild().combined_form

        as_the_page_renders_it = {
            name: field.initial or "" for name, field in combined.fields.items()
        }
        form = question_forms.PreventiveCareQuestions(as_the_page_renders_it)

        self.assertTrue(form.is_valid(), form.errors)

    def test_the_boilerplate_never_reaches_the_stored_answers(self):
        """qa_context goes into the model's medical context and into the
        regulator letter verbatim."""
        denial_type = self._preventive_care()
        combined = self._rebuild().combined_form

        # The person ticks one box and leaves every other box exactly as
        # the page rendered it: a prefill only reaches storage when nobody
        # types over it.
        posted = {
            name: field.initial
            for name, field in combined.fields.items()
            if field.initial
        }
        posted["trans_gender"] = "on"
        self._generate_appeal(**posted)

        stored = load_qa(self.denial_after_post())
        self.assertEqual(stored["trans_gender"], "on")
        self.assertNotIn(denial_type.appeal_text, stored.values())
        self.assertIn(stored.get("medical_reason", ""), (None, ""))

    def test_no_seeded_denial_type_prefills_a_box_it_would_then_reject(self):
        """Every seeded type, not just the one that broke."""
        for denial_type in DenialTypes.objects.exclude(form__isnull=True).exclude(
            form=""
        ):
            form_class = denial_type.get_form()
            if form_class is None:
                continue
            with self.subTest(denial_type=denial_type.name):
                self.denial.qa_context = ""
                self.denial.save(update_fields=["qa_context"])
                self.denial.denial_type.set([denial_type])

                combined = self._rebuild().combined_form

                for name, field in combined.fields.items():
                    self.assertIn(
                        field.initial,
                        (None, "", False),
                        f"{denial_type.name} pre-types {field.initial!r} into "
                        f"{name}, which the person never wrote",
                    )
                posted = {
                    name: field.initial or "" for name, field in combined.fields.items()
                }
                form = form_class(posted)
                self.assertTrue(
                    form.is_valid(),
                    f"{denial_type.name} renders a page its own form rejects: "
                    f"{form.errors}",
                )


class QuestionsOutcomeTest(QuestionsStepTestBase):
    """The page reports which of three real states it is in.

    Not the truthiness of the form: a Django Form with no fields is still
    truthy, so the old ``{% if combined %}`` could only ever take one branch.
    """

    def _render(self, outcome, combined):
        return render_to_string(
            "outside_help.html",
            {
                "outside_help_details": [],
                "combined": combined,
                "questions_outcome": outcome,
                "denial_form": "",
                "back_url": "/back",
                "back_label": "Back to review",
            },
        )

    def _no_questions_denial(self):
        self.denial.generated_questions = []
        self.denial.save(update_fields=["generated_questions"])
        self.denial.refresh_from_db()
        return self.denial

    def test_a_page_with_no_questions_reports_no_questions(self):
        info = FindNextStepsHelper.find_next_steps_for_denial(
            self._no_questions_denial(), self.email
        )
        # Why the outcome field exists at all: the form the template used to
        # branch on is truthy with nothing in it.
        self.assertEqual(info.combined_form.fields, {})
        self.assertTrue(info.combined_form)

        self.assertEqual(info.questions_outcome, QUESTIONS_OUTCOME_NONE)

    def test_a_page_with_questions_reports_questions(self):
        info = FindNextStepsHelper.find_next_steps_for_denial(self.denial, self.email)
        self.assertEqual(info.questions_outcome, QUESTIONS_OUTCOME_PRESENT)

    def test_a_run_that_did_not_finish_reports_that(self):
        """generate_appeal_questions returns None when it did not finish."""
        self._no_questions_denial()
        with patch(_QUESTIONS, new_callable=AsyncMock, return_value=None):
            info = FindNextStepsHelper.find_next_steps(
                denial_id=self.denial.denial_id,
                email=self.email,
                semi_sekret="sekret",
                procedure="",
                diagnosis="",
                insurance_company="",
                plan_id="",
                claim_id="",
                denial_type=None,
                denial_date=None,
            )
        self.assertEqual(info.questions_outcome, QUESTIONS_OUTCOME_UNFINISHED)

    def test_the_ml_helpers_own_none_reaches_the_page(self):
        """The distinction has to survive the ML helper boundary, not just
        the wrapper's exception handler: an inner deadline or an exhausted
        set of backends is not "we have what we need"."""
        self._no_questions_denial()
        with patch(_ML_HELPER, new_callable=AsyncMock, return_value=None):
            info = FindNextStepsHelper.find_next_steps(
                denial_id=self.denial.denial_id,
                email=self.email,
                semi_sekret="sekret",
                procedure="",
                diagnosis="",
                insurance_company="",
                plan_id="",
                claim_id="",
                denial_type=None,
                denial_date=None,
            )
        self.assertEqual(info.questions_outcome, QUESTIONS_OUTCOME_UNFINISHED)

    def test_a_run_that_found_nothing_to_ask_reports_no_questions(self):
        """[] and None are different answers and get different copy."""
        self._no_questions_denial()
        with patch(_QUESTIONS, new_callable=AsyncMock, return_value=[]):
            info = FindNextStepsHelper.find_next_steps(
                denial_id=self.denial.denial_id,
                email=self.email,
                semi_sekret="sekret",
                procedure="",
                diagnosis="",
                insurance_company="",
                plan_id="",
                claim_id="",
                denial_type=None,
                denial_date=None,
            )
        self.assertEqual(info.questions_outcome, QUESTIONS_OUTCOME_NONE)

    def test_an_empty_render_says_there_are_no_questions_for_this_case(self):
        combined = FindNextStepsHelper.find_next_steps_for_denial(
            self._no_questions_denial(), self.email
        ).combined_form

        body = self._render(QUESTIONS_OUTCOME_NONE, combined)

        self.assertIn("No extra questions for this case", body)
        self.assertNotIn("A few more questions", body)
        self.assertIn("Ask me some questions anyway", body)

    def test_a_timed_out_render_says_it_did_not_finish(self):
        combined = FindNextStepsHelper.find_next_steps_for_denial(
            self._no_questions_denial(), self.email
        ).combined_form

        body = self._render(QUESTIONS_OUTCOME_UNFINISHED, combined)

        self.assertIn("could not finish working out the questions", body)
        self.assertNotIn("A few more questions", body)
        self.assertIn("Ask me some questions anyway", body)

    def test_a_page_with_questions_keeps_the_questions_heading(self):
        combined = FindNextStepsHelper.find_next_steps_for_denial(
            self.denial, self.email
        ).combined_form

        body = self._render(QUESTIONS_OUTCOME_PRESENT, combined)

        self.assertIn("A few more questions", body)
        self.assertNotIn("No extra questions for this case", body)
        self.assertIn(_Q1, body)

    def test_a_render_with_no_outcome_does_not_invent_a_failure(self):
        """The dataclass defaults to "questions"; the template must not
        default to the most alarming copy of the three."""
        body = render_to_string(
            "outside_help.html",
            {
                "outside_help_details": [],
                "combined": None,
                "denial_form": "",
                "back_url": "/back",
            },
        )

        self.assertNotIn("could not finish working out the questions", body)
        self.assertNotIn("No extra questions for this case", body)
        self.assertNotIn("A few more questions", body)

    def test_a_render_with_no_outcome_still_shows_questions_it_has(self):
        combined = FindNextStepsHelper.find_next_steps_for_denial(
            self.denial, self.email
        ).combined_form

        body = render_to_string(
            "outside_help.html",
            {
                "outside_help_details": [],
                "combined": combined,
                "denial_form": "",
                "back_url": "/back",
            },
        )

        self.assertIn("A few more questions", body)
        self.assertIn(_Q1, body)
        self.assertNotIn("could not finish working out the questions", body)


class LoadingPageTest(QuestionsStepTestBase):
    """The 8 second reveal must not offer a second submit.

    find_next_steps is not idempotent and the auto-submitted POST is still in
    flight at 8 seconds, so a control revealed then would be a duplicate
    submission of the same payload.
    """

    _OPENS_A_CALLBACK = "window.setTimeout(function () {"
    _SUBMIT_CALL = re.compile(r"\.(?:submit|requestSubmit)\s*\(")
    _LOOP = re.compile(r"\b(?:for|while)\s*\(|\.forEach\s*\(")

    def _loading_page(self):
        response = self.client.post(reverse("find_next_steps_loading"), self._ref())
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    _SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL)

    def _script(self, body):
        """The page's own script block, not whichever one base.html emits first."""
        blocks = [
            block
            for block in self._SCRIPT_BLOCK.findall(body)
            if "next-steps-form" in block
        ]
        self.assertEqual(
            len(blocks), 1, "expected exactly one script driving the loading page"
        )
        return blocks[0]

    def _callback_registered_at(self, body, delay_ms):
        """The body of the setTimeout callback registered for ``delay_ms``.

        Reading the callback, not the text near the number: an identifier
        that merely appears close to "8000" says nothing about whether the
        timer that fires then does anything with it.
        """
        script = self._script(body)
        before, marker, _ = script.partition(f"}}, {delay_ms});")
        self.assertTrue(marker, f"no setTimeout registered at {delay_ms}ms")
        opened = before.rfind(self._OPENS_A_CALLBACK)
        self.assertNotEqual(opened, -1, f"the {delay_ms}ms timer takes no callback")
        return before[opened + len(self._OPENS_A_CALLBACK) :]

    def test_the_explanation_starts_hidden(self):
        body = self._loading_page()
        block = body.split('id="slow-explanation"', 1)[1].split(">", 1)[0]
        self.assertIn("display:none", block)

    def test_the_explanation_is_revealed_at_eight_seconds(self):
        """The 8s callback shows the explanation, not merely name it."""
        callback = self._callback_registered_at(self._loading_page(), 8000)
        self.assertIn("slow-explanation", callback)
        self.assertIn("display = 'block'", callback)
        self.assertNotIn("manual-continue", callback)
        self.assertNotIn("manualContinue", callback)

    def test_the_submit_control_still_waits_for_twenty_seconds(self):
        """The manual Continue button is revealed by the 20s callback and by
        no earlier one."""
        body = self._loading_page()

        twenty = self._callback_registered_at(body, 20000)
        self.assertIn("manualContinue", twenty)
        self.assertIn("display = 'block'", twenty)

        eight = self._callback_registered_at(body, 8000)
        self.assertNotIn("manualContinue", eight)

    def test_the_eight_second_block_offers_a_way_back_and_no_submit(self):
        body = self._loading_page()
        block = body.split('id="slow-explanation"', 1)[1].split("</div>", 1)[0]
        self.assertIn(reverse("categorize_review"), block)
        self.assertNotIn("<button", block)
        self.assertNotIn('type="submit"', block)

    def test_the_page_submits_find_next_steps_exactly_once(self):
        """One submission call in the whole script, and no loop around it.

        Counting a literal ``form.submit()`` missed both ways of adding a
        second POST of the same non-idempotent payload: another call spelt
        ``requestSubmit``, or a loop around the one that is there.
        """
        script = self._script(self._loading_page())

        self.assertEqual(
            len(self._SUBMIT_CALL.findall(script)),
            1,
            "the loading script submits the payload more than once",
        )
        self.assertIsNone(
            self._LOOP.search(script),
            "a loop in the loading script can repeat the submission",
        )


class MagicCombinedFormTest(TestCase):
    """The merge itself, on the two shapes that used to break it."""

    def test_a_shared_field_name_does_not_raise_or_concatenate(self):
        """``initial += initial`` raised when the kept field had no default,
        and find_next_steps recovered from that raise by rebuilding the form
        with an empty answers dict."""

        class First(django_forms.Form):
            medical_reason = django_forms.CharField(required=False)

        class Second(django_forms.Form):
            medical_reason = django_forms.CharField(
                required=False, initial="the second type's default"
            )

        combined = magic_combined_form([First(), Second()], {})

        self.assertEqual(
            combined.fields["medical_reason"].initial, "the second type's default"
        )

    def test_a_stored_answer_beats_both_defaults(self):
        class First(django_forms.Form):
            medical_reason = django_forms.CharField(
                required=False, initial="the first type's default"
            )

        class Second(django_forms.Form):
            medical_reason = django_forms.CharField(
                required=False, initial="the second type's default"
            )

        combined = magic_combined_form(
            [First(), Second()], {"medical_reason": "what the person typed"}
        )

        self.assertEqual(
            combined.fields["medical_reason"].initial, "what the person typed"
        )

    def test_a_per_form_initial_reaches_the_field(self):
        """``SomeForm(initial={...})`` lives on the form, not on the field,
        so ``field.initial`` cannot see it and ``get_initial_for_field`` is
        the accessor that reads both."""

        class WithDefault(django_forms.Form):
            medical_reason = django_forms.CharField(required=False)

        combined = magic_combined_form(
            [WithDefault(initial={"medical_reason": "from the denial type"})], {}
        )

        self.assertEqual(
            combined.fields["medical_reason"].initial, "from the denial type"
        )

    def test_a_checkbox_stored_as_on_comes_back_ticked(self):
        """A ticked checkbox posts "on", never "True"."""

        class WithBox(django_forms.Form):
            urgent = django_forms.BooleanField(required=False)

        self.assertIs(
            magic_combined_form([WithBox()], {"urgent": "on"}).fields["urgent"].initial,
            True,
        )
        self.assertIs(
            magic_combined_form([WithBox()], {"urgent": "False"})
            .fields["urgent"]
            .initial,
            False,
        )

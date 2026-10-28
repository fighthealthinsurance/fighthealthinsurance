"""find_next_steps must never lose data it was just given (or data a
concurrent extraction wrote).

Pinned regressions:
- the appeal fax number was written via .update() and then reverted by the
  stale full-row denial.save() a few lines later;
- the full-row save also clobbered any field a concurrent entity-extraction
  task persisted between the load and the save (now save(update_fields=...));
- blank form fields ("" from required=False inputs, empty querysets from
  ModelMultipleChoiceField) overwrote real stored values with nothing;
- extraction's own aupdate overwrote the USER's confirmed procedure/diagnosis
  when the LLM call finished after the user had typed their own values.

The blank-submit rule has one exception: a box the review page showed filled
(it names those in ``shown_filled``) and the person emptied is cleared, and
unticking every box of a list the page showed ticked clears that list. The
case remembers what was emptied, so a read of the letter that finishes later
leaves it empty, until a new letter replaces this one.
"""

from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.common_view_logic import (
    DenialCreatorHelper,
    FindNextStepsHelper,
)
from fighthealthinsurance.models import (
    DataSource,
    Denial,
    DenialTypes,
    InsuranceCompany,
    InsurancePlan,
    PlanSource,
)

_QUESTIONS = (
    "fighthealthinsurance.common_view_logic.DenialCreatorHelper."
    "generate_appeal_questions"
)


class FindNextStepsDataIntegrityTest(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/plan_source.yaml"]

    def setUp(self):
        self.email = "integrity@example.com"
        self.denial = Denial.objects.create(
            denial_id=9001,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(self.email),
            denial_text="Denied a CGM for T2D.",
            procedure="CGM",
            diagnosis="type 2 diabetes",
            insurance_company="Aetna",
            plan_id="PLN-1",
            claim_id="CLM-1",
        )
        self.denial.plan_source.set([PlanSource.objects.first()])

    def _find_next_steps(self, **overrides):
        kwargs = dict(
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
        kwargs.update(overrides)
        with patch(_QUESTIONS):
            return FindNextStepsHelper.find_next_steps(**kwargs)

    def test_appeal_fax_number_survives_the_save(self):
        self._find_next_steps(appeal_fax_number="555-000-1111")
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.appeal_fax_number, "555-000-1111")

    def test_blank_submit_does_not_clobber_stored_fields(self):
        self._find_next_steps()
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.procedure, "CGM")
        self.assertEqual(self.denial.diagnosis, "type 2 diabetes")
        self.assertEqual(self.denial.insurance_company, "Aetna")
        self.assertEqual(self.denial.plan_id, "PLN-1")
        self.assertEqual(self.denial.claim_id, "CLM-1")
        self.assertEqual(self.denial.plan_source.count(), 1)

    def test_concurrent_extraction_write_is_not_reverted(self):
        # Simulate an extraction task landing between the helper's load and
        # its save: _get_outside_help_details runs exactly in that window, so
        # piggyback the background write on it. The fax field is not touched
        # by this form POST, so the update_fields save must leave it alone.
        real_helper = FindNextStepsHelper._get_outside_help_details

        def write_then_continue(denial, your_state=None):
            Denial.objects.filter(denial_id=denial.denial_id).update(
                appeal_fax_number="555-222-3333"
            )
            return real_helper(denial, your_state)

        with patch.object(
            FindNextStepsHelper,
            "_get_outside_help_details",
            side_effect=write_then_continue,
        ):
            self._find_next_steps(procedure="Updated procedure")
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.appeal_fax_number, "555-222-3333")
        self.assertEqual(self.denial.procedure, "Updated procedure")

    def test_a_box_shown_filled_and_emptied_is_cleared(self):
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            date_of_service="2024-01-15", your_state="CA", state="CA"
        )
        for name in (
            "procedure",
            "diagnosis",
            "insurance_company",
            "plan_id",
            "claim_id",
            "date_of_service",
            "your_state",
        ):
            with self.subTest(field=name):
                self._find_next_steps(shown_filled={name})
                self.denial.refresh_from_db()
                self.assertIsNone(getattr(self.denial, name))

    def test_a_cleared_state_clears_its_mirror_too(self):
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            your_state="CA", state="CA"
        )
        self._find_next_steps(shown_filled={"your_state"})
        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.state)

    def test_a_box_shown_empty_keeps_a_value_that_arrived_since(self):
        # The page named no field as filled: whatever the row holds now came
        # after the page was drawn, and an empty box says nothing about it.
        self._find_next_steps(shown_filled=set())
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.claim_id, "CLM-1")

    def test_unticking_every_plan_source_shown_clears_them(self):
        self._find_next_steps(shown_filled={"plan_source"}, plan_source=[])
        self.assertEqual(self.denial.plan_source.count(), 0)

    def test_unticking_every_denial_type_shown_clears_them(self):
        denial_type = DenialTypes.objects.create(
            name="Medical necessity (test)", regex="necessary", negative_regex=""
        )
        self.denial.denial_type.set([denial_type])
        self._find_next_steps(shown_filled={"denial_type"}, denial_type=[])
        self.assertEqual(self.denial.denial_type.count(), 0)

    def a_matched_type(self, name):
        return DenialTypes.objects.create(name=name, regex="CGM", negative_regex="")

    def read_the_types_late(self, *matched):
        """extract_set_denialtype finishing after the review was submitted,
        with the letter matching ``matched``."""
        DataSource.objects.get_or_create(name="regex")
        with (
            patch.object(DenialCreatorHelper, "_regex_src", None),
            patch.object(
                DenialCreatorHelper.regex_denial_processor,
                "get_denialtype",
                new_callable=AsyncMock,
                return_value=list(matched),
            ),
        ):
            async_to_sync(DenialCreatorHelper.extract_set_denialtype)(
                self.denial.denial_id
            )

    def test_a_late_plan_id_read_leaves_a_cleared_box_empty(self):
        self._find_next_steps(shown_filled={"plan_id"})
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator.get_plan_id",
            return_value="ABC123456789",
        ):
            async_to_sync(DenialCreatorHelper.extract_set_plan_id)(
                self.denial.denial_id
            )
        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.plan_id)

    def test_a_late_read_of_the_letter_leaves_a_cleared_procedure_empty(self):
        self._find_next_steps(shown_filled={"procedure"})
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator."
            "get_procedure_and_diagnosis",
            return_value=("CGM", "type 2 diabetes"),
        ):
            async_to_sync(DenialCreatorHelper.extract_set_denial_and_diagnosis)(
                self.denial.denial_id
            )
        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.procedure)

    def test_a_late_plan_match_leaves_a_cleared_plan_empty(self):
        insurer = InsuranceCompany.objects.create(name="Example Health")
        plan = InsurancePlan.objects.create(
            insurance_company=insurer, plan_name="Example Gold PPO"
        )
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            insurance_company="Example Health",
            insurance_company_obj=insurer,
            insurance_plan_obj=plan,
        )
        self._find_next_steps(shown_filled={"insurance_plan_obj"})
        async_to_sync(DenialCreatorHelper._store_plan_if_it_is_the_rows_carriers)(
            self.denial.denial_id, plan
        )
        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.insurance_plan_obj)

    def test_a_late_read_of_the_letter_adds_no_types_after_all_were_unticked(self):
        matched = self.a_matched_type("Medical necessity (test)")
        self.denial.denial_type.set([matched])
        self._find_next_steps(shown_filled={"denial_type"}, denial_type=[])
        self.read_the_types_late(matched)
        self.assertEqual(self.denial.denial_type.count(), 0)

    def test_a_late_read_of_the_letter_does_not_add_back_an_unticked_type(self):
        kept = self.a_matched_type("Medical necessity (test)")
        unticked = self.a_matched_type("Out of network (test)")
        self.denial.denial_type.set([kept, unticked])
        self._find_next_steps(shown_filled={"denial_type"}, denial_type=[kept])
        self.read_the_types_late(kept, unticked)
        self.assertEqual(list(self.denial.denial_type.all()), [kept])

    def test_a_new_letter_is_read_afresh_for_what_was_cleared(self):
        self._find_next_steps(shown_filled={"plan_id"})
        self.denial.refresh_from_db()
        DenialCreatorHelper._invalidate_denial_text_artifacts(self.denial)
        self.denial.refresh_from_db()
        self.assertFalse(self.denial.cleared_on_review("plan_id"))

    def test_non_json_qa_context_does_not_500(self):
        self.denial.qa_context = "free text from a historical row"
        self.denial.save(update_fields=["qa_context"])
        info = self._find_next_steps()
        self.assertIsNotNone(info)


class ExtractionRespectsUserValuesTest(TestCase):
    def test_extraction_only_fills_empty_procedure_and_diagnosis(self):
        denial = Denial.objects.create(
            denial_id=9002,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email("user@example.com"),
            denial_text="Denied an MRI.",
            # The user confirmed this while the LLM call was in flight.
            procedure="user-confirmed procedure",
            diagnosis="",
        )
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator."
            "get_procedure_and_diagnosis",
            return_value=("model guessed procedure", "model guessed diagnosis"),
        ):
            async_to_sync(DenialCreatorHelper.extract_set_denial_and_diagnosis)(
                denial.denial_id
            )
        denial.refresh_from_db()
        # User's value kept; the empty field filled by the model.
        self.assertEqual(denial.procedure, "user-confirmed procedure")
        self.assertEqual(denial.diagnosis, "model guessed diagnosis")
        # The candidate mirrors still record what the model said.
        self.assertEqual(denial.candidate_procedure, "model guessed procedure")
        self.assertTrue(denial.extract_procedure_diagnosis_finished)

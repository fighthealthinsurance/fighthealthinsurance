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
"""

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.common_view_logic import (
    DenialCreatorHelper,
    FindNextStepsHelper,
)
from fighthealthinsurance.models import Denial, PlanSource

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

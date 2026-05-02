"""Tests for specialized denial-type templates and the make_appeals
specialized hint path.

Specialized templates auto-cite the laws/regulations US News flagged as
common appeal grounds (mental-health parity, advanced imaging, specialty
meds, PT continuation, post-surgical rehab) and seed a single extra call
to the highest-quality internal model so the strongest model gets a
chance to use the targeted citations.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fighthealthinsurance.generate_appeal import (
    AdvancedImagingAppeal,
    AppealGenerator,
    AppealTemplateGenerator,
    MentalHealthParityAppeal,
    PhysicalTherapyContinuationAppeal,
    PostSurgicalRehabAppeal,
    SPECIALIZED_DENIAL_TEMPLATES,
    SpecialtyMedicationAppeal,
    detect_specialized_templates,
)
from fighthealthinsurance.ml.ml_models import RemoteModelLike


class TestMentalHealthParityAppealMatches(unittest.TestCase):
    def test_matches_residential_treatment_text(self):
        self.assertTrue(
            MentalHealthParityAppeal.matches(
                "We are denying coverage for residential treatment.",
            )
        )

    def test_matches_substance_use_phrase(self):
        self.assertTrue(
            MentalHealthParityAppeal.matches(
                "Claim denied for inpatient substance use disorder care.",
            )
        )

    def test_matches_diagnosis_only(self):
        self.assertTrue(
            MentalHealthParityAppeal.matches(
                "Claim denied.",
                diagnosis="Major depressive disorder",
            )
        )

    def test_does_not_match_orthopedic_denial(self):
        self.assertFalse(
            MentalHealthParityAppeal.matches(
                "Knee replacement is not medically necessary.",
                procedure="Total knee arthroplasty",
                diagnosis="Osteoarthritis",
            )
        )

    def test_static_appeal_cites_required_laws(self):
        text = MentalHealthParityAppeal.static_appeal()
        self.assertIn("MHPAEA", text)
        self.assertIn("29 U.S.C. § 1185a", text)
        self.assertIn("CMS 2024 Final Rule", text)
        self.assertIn("42 U.S.C. § 18022(b)(1)(E)", text)

    def test_static_appeal_uses_substitution_placeholders(self):
        text = MentalHealthParityAppeal.static_appeal()
        # These are filled in downstream by sub_in_appeals
        self.assertIn("{insurance_company}", text)
        self.assertIn("{claim_id}", text)
        self.assertIn("{procedure}", text)
        self.assertIn("{diagnosis}", text)

    def test_model_prompt_hint_includes_citations(self):
        hint = MentalHealthParityAppeal.model_prompt_hint()
        self.assertIn("MHPAEA", hint)
        self.assertIn("CMS 2024 Final Rule", hint)
        self.assertIn("Mental Health Parity", hint)


class TestAdvancedImagingAppealMatches(unittest.TestCase):
    def test_matches_mri_in_text(self):
        self.assertTrue(
            AdvancedImagingAppeal.matches(
                "MRI of the lumbar spine is denied.",
            )
        )

    def test_matches_ct_scan(self):
        self.assertTrue(
            AdvancedImagingAppeal.matches(
                "CT scan is not medically necessary.",
            )
        )

    def test_matches_pet_scan_phrase(self):
        self.assertTrue(
            AdvancedImagingAppeal.matches(
                "We have denied coverage for the requested PET scan.",
            )
        )

    def test_does_not_match_unrelated_denial(self):
        self.assertFalse(
            AdvancedImagingAppeal.matches(
                "Therapy session limit has been reached.",
            )
        )

    def test_static_appeal_cites_acr_and_cms(self):
        text = AdvancedImagingAppeal.static_appeal()
        self.assertIn("ACR Appropriateness Criteria", text)
        self.assertIn("CMS National Coverage Determinations", text)
        self.assertIn("CMS 2024 Final Rule", text)


class TestSpecialtyMedicationAppealMatches(unittest.TestCase):
    def test_matches_step_therapy(self):
        self.assertTrue(
            SpecialtyMedicationAppeal.matches(
                "Patient must complete step therapy before this drug is approved.",
            )
        )

    def test_matches_brand_name_biologic(self):
        self.assertTrue(
            SpecialtyMedicationAppeal.matches(
                "Humira is non-formulary; denied.",
            )
        )

    def test_matches_glp1(self):
        self.assertTrue(
            SpecialtyMedicationAppeal.matches(
                "Wegovy is denied because patient does not meet criteria.",
            )
        )

    def test_static_appeal_mentions_step_therapy_and_erisa(self):
        text = SpecialtyMedicationAppeal.static_appeal()
        self.assertIn("step therapy", text.lower())
        self.assertIn("ERISA", text)
        self.assertIn("CMS 2024 Final Rule", text)


class TestPhysicalTherapyContinuationAppealMatches(unittest.TestCase):
    def test_matches_visit_limit(self):
        self.assertTrue(
            PhysicalTherapyContinuationAppeal.matches(
                "Patient has reached the maximum number of visits.",
            )
        )

    def test_matches_plateau(self):
        self.assertTrue(
            PhysicalTherapyContinuationAppeal.matches(
                "Physical therapy is denied because the patient has plateaued.",
            )
        )

    def test_static_appeal_cites_jimmo_and_aca(self):
        text = PhysicalTherapyContinuationAppeal.static_appeal()
        self.assertIn("Jimmo", text)
        self.assertIn("42 U.S.C. § 18022(b)(1)(G)", text)
        self.assertIn("CMS 2024 Final Rule", text)


class TestPostSurgicalRehabAppealMatches(unittest.TestCase):
    def test_matches_inpatient_rehab(self):
        self.assertTrue(
            PostSurgicalRehabAppeal.matches(
                "Inpatient rehabilitation is denied as not medically necessary.",
            )
        )

    def test_matches_skilled_nursing(self):
        self.assertTrue(
            PostSurgicalRehabAppeal.matches(
                "Skilled nursing facility stay is denied.",
            )
        )

    def test_matches_post_op_rehab_phrase(self):
        self.assertTrue(
            PostSurgicalRehabAppeal.matches(
                "Post-surgical rehab following the procedure is denied.",
            )
        )

    def test_static_appeal_cites_cms_chapters(self):
        text = PostSurgicalRehabAppeal.static_appeal()
        self.assertIn("Medicare Benefit Policy Manual", text)
        self.assertIn("Jimmo", text)
        self.assertIn("CMS 2024 Final Rule", text)


class TestDetectSpecializedTemplates(unittest.TestCase):
    def test_detect_returns_empty_for_empty_inputs(self):
        self.assertEqual(detect_specialized_templates(None), [])
        self.assertEqual(detect_specialized_templates(""), [])

    def test_detect_picks_mental_health_parity(self):
        matches = detect_specialized_templates(
            "Inpatient psychiatric admission denied.",
        )
        self.assertIn(MentalHealthParityAppeal, matches)

    def test_detect_picks_advanced_imaging(self):
        matches = detect_specialized_templates(
            "MRI of the brain is denied.",
        )
        self.assertIn(AdvancedImagingAppeal, matches)

    def test_detect_picks_pt_continuation(self):
        matches = detect_specialized_templates(
            "Physical therapy plateau noted; visits will end.",
        )
        self.assertIn(PhysicalTherapyContinuationAppeal, matches)

    def test_detect_picks_specialty_meds(self):
        matches = detect_specialized_templates(
            "Humira step therapy required.",
        )
        self.assertIn(SpecialtyMedicationAppeal, matches)

    def test_detect_picks_post_surgical_rehab(self):
        matches = detect_specialized_templates(
            "Skilled nursing stay following hip replacement is denied.",
        )
        self.assertIn(PostSurgicalRehabAppeal, matches)

    def test_detect_can_return_multiple(self):
        # Mental-health residential is itself a behavioral-health denial
        # (parity) AND can match the post-acute rehab template via
        # "residential treatment" → not — but a clear multi-match: PT
        # continuation triggered by a denial that mentions both PT visit
        # limits AND substance use disorder context for an addiction
        # patient gets both behavioral-health and PT.
        matches = detect_specialized_templates(
            "Maximum number of visits reached for substance use disorder "
            "outpatient therapy.",
        )
        self.assertIn(MentalHealthParityAppeal, matches)
        self.assertIn(PhysicalTherapyContinuationAppeal, matches)

    def test_detect_returns_empty_for_non_matching_denial(self):
        matches = detect_specialized_templates(
            "Routine annual checkup paid in full; no denial.",
        )
        self.assertEqual(matches, [])

    def test_specialized_registry_contains_all_five_templates(self):
        names = {t.__name__ for t in SPECIALIZED_DENIAL_TEMPLATES}
        self.assertEqual(
            names,
            {
                "MentalHealthParityAppeal",
                "AdvancedImagingAppeal",
                "SpecialtyMedicationAppeal",
                "PhysicalTherapyContinuationAppeal",
                "PostSurgicalRehabAppeal",
            },
        )


class TestBuildSpecializedHintBlock(unittest.TestCase):
    def test_combines_multiple_hints(self):
        block = AppealGenerator._build_specialized_hint_block(
            [MentalHealthParityAppeal, AdvancedImagingAppeal]
        )
        self.assertIn("MHPAEA", block)
        self.assertIn("American College of Radiology", block)

    def test_deduplicates_by_name(self):
        # Passing the same template twice should not duplicate the hint;
        # the per-template "involves <name>" preamble is unique per template
        # and a clean counter for dedup.
        block = AppealGenerator._build_specialized_hint_block(
            [MentalHealthParityAppeal, MentalHealthParityAppeal]
        )
        self.assertEqual(block.count("involve Mental Health Parity"), 1)

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(AppealGenerator._build_specialized_hint_block([]), "")


class TestBestInternalModelName(unittest.TestCase):
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_when_no_internal_model(self, mock_router):
        mock_router.best_internal_model.return_value = None
        mock_router.models_by_name = {}
        self.assertIsNone(AppealGenerator._best_internal_model_name())

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_friendly_name_for_best_model(self, mock_router):
        best = MagicMock(spec=RemoteModelLike)
        other = MagicMock(spec=RemoteModelLike)
        mock_router.best_internal_model.return_value = best
        mock_router.models_by_name = {
            "some-other": [other],
            "fhi-2025": [best],
        }
        self.assertEqual(AppealGenerator._best_internal_model_name(), "fhi-2025")


class TestMakeAppealsSpecializedHint(unittest.TestCase):
    """When specialized_templates is passed, make_appeals must invoke the
    helpers that build a single extra call targeting the highest-quality
    internal model.

    We verify this by spying on the helper methods rather than the (deep,
    closure-bound) call list, which keeps the test focused on the
    observable contract.
    """

    def _build_denial(self):
        return SimpleNamespace(
            denial_text="Inpatient psychiatric admission denied.",
            procedure="Inpatient psychiatric admission",
            diagnosis="Major depressive disorder",
            patient_user=None,
            primary_professional=None,
            qa_context=None,
            health_history=None,
            professional_to_finish=False,
            plan_id=None,
            claim_id="ABC123",
            insurance_company="Acme Health",
            insurance_company_obj=None,
            plan_context=None,
            plan_documents_summary=None,
            use_external=False,
            denial_id=42,
        )

    def _run_make_appeals(self, gen, denial, **kwargs):
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router"
        ) as mock_router, patch(
            "fighthealthinsurance.generate_appeal.as_available_nested",
            return_value=iter([]),
        ):
            best = MagicMock(spec=RemoteModelLike)
            best.quality.return_value = 200
            mock_router.generate_text_backend_names.return_value = []
            mock_router.best_internal_model.return_value = best
            mock_router.models_by_name = {"fhi-best": [best]}
            template_gen = AppealTemplateGenerator([], [], [])
            list(gen.make_appeals(denial, template_gen, **kwargs))

    def test_helpers_called_when_specialized_templates_given(self):
        gen = AppealGenerator()
        denial = self._build_denial()
        with patch.object(
            AppealGenerator,
            "_best_internal_model_name",
            return_value="fhi-best",
        ) as best_spy, patch.object(
            AppealGenerator,
            "_build_specialized_hint_block",
            return_value="HINTS",
        ) as block_spy:
            self._run_make_appeals(
                gen,
                denial,
                specialized_templates=[MentalHealthParityAppeal],
            )
            block_spy.assert_called_once_with([MentalHealthParityAppeal])
            best_spy.assert_called_once()

    def test_helpers_not_called_when_no_specialized_templates(self):
        gen = AppealGenerator()
        denial = self._build_denial()
        with patch.object(
            AppealGenerator, "_best_internal_model_name"
        ) as best_spy, patch.object(
            AppealGenerator, "_build_specialized_hint_block"
        ) as block_spy:
            self._run_make_appeals(gen, denial, specialized_templates=None)
            best_spy.assert_not_called()
            block_spy.assert_not_called()

    def test_specialized_branch_skipped_when_no_best_model(self):
        gen = AppealGenerator()
        denial = self._build_denial()
        with patch.object(
            AppealGenerator, "_best_internal_model_name", return_value=None
        ), patch.object(AppealGenerator, "_build_specialized_hint_block") as block_spy:
            self._run_make_appeals(
                gen,
                denial,
                specialized_templates=[MentalHealthParityAppeal],
            )
            # Without a best internal model we don't bother building hints.
            block_spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

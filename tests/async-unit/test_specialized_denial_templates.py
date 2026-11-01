"""Tests for specialized denial-type templates and the make_appeals
specialized hint path.

Specialized templates auto-cite the laws/regulations US News flagged as
common appeal grounds (mental-health parity, advanced imaging, specialty
meds, PT continuation, post-surgical rehab) and seed a single extra call
to the highest-quality internal model so the strongest model gets a
chance to use the targeted citations.
"""

import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fighthealthinsurance.generate_appeal import (
    AdvancedImagingAppeal,
    AppealGenerator,
    AppealTemplateGenerator,
    GLP1WeightLossAppeal,
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
        self.assertIn("42 U.S.C. § 300gg-26", text)
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
        self.assertIn("Medicare Advantage plans only", hint)
        self.assertIn("Mental Health Parity", hint)


# Signs the text leans on the 2024 MHPAEA rule's new provisions.
_PARITY_2024_RULE_MARKERS = (
    r"89 FR 77586",
    r"2024[^.;]*(?:MHPAEA|[Pp]arity|NQTL|DOL)",
    r"(?:MHPAEA|[Pp]arity|NQTL|DOL)[^.;]*2024",
    r"2590\.712-1|146\.137|54\.9812-2",
    r"no more restrictive",
    r"meaningful benefit",
    r"material difference",
    r"network composition",
)


def _paragraph(text, start):
    """The letter paragraph that begins with ``start``."""
    for paragraph in text.split("\n\n"):
        if paragraph.startswith(start):
            return paragraph
    raise AssertionError(f"no paragraph starts with {start!r}")


class TestParityTemplateRestsOnEnforcedLaw(unittest.TestCase):
    """On May 15, 2025 the Departments said they will not enforce the 2024
    MHPAEA rule's new provisions, so the letter and the model's citations
    rest on the statute and the 2013 rule."""

    def test_letter_and_citations_do_not_rely_on_the_2024_rule(self):
        texts = (MentalHealthParityAppeal.static_appeal(),)
        texts += MentalHealthParityAppeal.citations
        for text in texts:
            for marker in _PARITY_2024_RULE_MARKERS:
                with self.subTest(marker=marker, text=text[:60]):
                    self.assertNotRegex(text, marker)

    def test_hint_names_the_2024_rule_only_to_rule_it_out(self):
        hint = MentalHealthParityAppeal.model_prompt_hint()
        self.assertIn("Do not cite the 2024 MHPAEA final rule", hint)
        self.assertIn("May 15, 2025", hint)

    def test_letter_cites_the_statute_for_employer_and_individual_plans(self):
        text = MentalHealthParityAppeal.static_appeal()
        self.assertIn("ERISA § 712, 29 U.S.C. § 1185a", text)
        self.assertIn("Public Health Service Act § 2726, 42 U.S.C. § 300gg-26", text)

    def test_letter_asks_for_the_statutory_comparative_analysis(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "2. ")
        self.assertIn("comparative analysis", paragraph)
        self.assertIn("29 U.S.C. § 1185a(a)(8)", paragraph)
        self.assertIn("42 U.S.C. § 300gg-26(a)(8)", paragraph)
        self.assertIn("FAQs Part 45, Q6", paragraph)

    def test_letter_uses_the_2013_rule_standard(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "1. ")
        self.assertIn("comparable to, and applied no more stringently than", paragraph)
        self.assertIn("29 CFR 2590.712(c)(4)(i)", paragraph)
        self.assertIn("45 CFR 146.136(c)(4)(i)", paragraph)

    def test_letter_bans_limits_that_apply_only_to_mental_health_care(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "1. ")
        self.assertIn("may not impose limits that apply only to this care", paragraph)
        self.assertIn("29 U.S.C. § 1185a(a)(3)(A)(ii)", paragraph)
        self.assertIn("42 U.S.C. § 300gg-26(a)(3)(A)(ii)", paragraph)

    def test_letter_stays_short(self):
        # It grew to almost 900 words; keep it readable.
        self.assertLess(len(MentalHealthParityAppeal.static_appeal().split()), 600)

    def test_hint_pins_the_2013_test_to_paragraph_c_4_i(self):
        rule = [c for c in MentalHealthParityAppeal.citations if "2013" in c]
        self.assertEqual(len(rule), 1)
        self.assertIn("(paragraph (c)(4)(i))", rule[0])

    def test_claims_rule_applies_directly_only_to_erisa_plans(self):
        opening = _paragraph(
            MentalHealthParityAppeal.static_appeal(), "I am appealing"
        )
        self.assertIn("private employer plans covered by ERISA", opening)

    def test_criteria_request_cites_both_disclosure_rules(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "2. ")
        self.assertIn("29 CFR 2590.712(d); 45 CFR 146.136(d)", paragraph)

    def test_erisa_document_request_also_goes_to_the_plan_administrator(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "2. ")
        self.assertIn(
            "sending a copy to the plan administrator named in my summary plan "
            "description",
            paragraph,
        )

    def test_essential_health_benefits_are_limited_to_the_plans_they_bind(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "4. ")
        self.assertIn("42 U.S.C. § 18022(b)(1)(E)", paragraph)
        self.assertIn("non-grandfathered individual, Marketplace, or small-group", paragraph)
        ehb = [c for c in MentalHealthParityAppeal.citations if "18022" in c]
        self.assertEqual(len(ehb), 1)
        self.assertIn("non-grandfathered individual and small-group", ehb[0])

    def test_letter_says_parity_law_does_not_reach_medicare_advantage(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "5. ")
        self.assertIn("Medicare Advantage plan, federal parity law does not apply", paragraph)
        self.assertIn("42 CFR 438.910", paragraph)

    def test_letter_has_no_em_dashes(self):
        self.assertNotIn("\u2014", MentalHealthParityAppeal.static_appeal())


class TestParityLetterScopesCommercialRules(unittest.TestCase):
    """The letter is picked before the plan type is known, so each duty names its plans."""

    def test_opening_limits_parity_and_claims_rules_to_the_plans_they_bind(self):
        opening = _paragraph(
            MentalHealthParityAppeal.static_appeal(), "I am appealing"
        )
        self.assertIn(
            "For most employer, individual, Marketplace, and state or local "
            "government plans, federal parity law and the federal claims rule "
            "govern this claim",
            opening,
        )

    def test_letter_has_no_paragraph_cross_references(self):
        self.assertNotRegex(
            MentalHealthParityAppeal.static_appeal(), r"(?i)\bparagraphs? \d"
        )

    def test_comparative_analysis_duty_is_limited_to_plans_under_mhpaea(self):
        paragraph = _paragraph(MentalHealthParityAppeal.static_appeal(), "2. ")
        self.assertIn("A plan subject to MHPAEA must document", paragraph)

    def test_external_review_request_does_not_assume_a_commercial_plan(self):
        closing = _paragraph(
            MentalHealthParityAppeal.static_appeal(), "I respectfully request"
        )
        self.assertIn(
            "the next level of review available to me, including any "
            "independent external review my plan must offer",
            closing,
        )


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
        self.assertIn("Medicare Advantage", text)


class TestImagingClaimsAreConditional(unittest.TestCase):
    """The imaging template can't know the case meets a criterion, so it
    must not say so: those claims are fill-ins, and the hint frames ACR and
    the NCD chapter as places to check."""

    # A sentence that asserts the study meets or satisfies a criterion.
    _CLAIM = re.compile(
        r"[^.\[\]]*\b(?:meets|satisfies)\b[^.\[\]]*"
        r"(?:indication|criteri|NCD|ACR)[^.\[\]]*\.",
        re.IGNORECASE,
    )

    def test_letter_makes_no_unconditional_criteria_claim(self):
        text = AdvancedImagingAppeal.static_appeal()
        # Bracketed fill-ins are the user's to complete; drop them first.
        outside_fill_ins = re.sub(r"\[[^\]]*\]", "", text)
        self.assertEqual(self._CLAIM.findall(outside_fill_ins), [])

    def test_letter_asks_which_criterion_was_applied(self):
        text = AdvancedImagingAppeal.static_appeal()
        self.assertIn("identify the criterion you applied", text)
        self.assertIn("[Specify the ACR Appropriateness Criteria scenario", text)

    def test_ncd_chapter_is_scoped_to_medicare_plans(self):
        text = AdvancedImagingAppeal.static_appeal()
        self.assertIn("If my plan is a Medicare plan", text)
        self.assertNotIn("community standard", text)

    def test_hint_frames_authorities_as_places_to_check(self):
        hint = AdvancedImagingAppeal.model_prompt_hint()
        self.assertIn("cite a specific NCD only if one fits", hint)
        self.assertIn("usually appropriate for the stated indication", hint)
        self.assertIn("Do not state that the study meets ACR criteria", hint)


class TestPatientSpecificClaimsAreFillIns(unittest.TestCase):
    def test_step_therapy_override_asks_which_ground_applies(self):
        text = SpecialtyMedicationAppeal.static_appeal()
        self.assertNotIn("the patient qualifies for an override", text)
        self.assertIn("[Specify which applies", text)

    def test_glp1_indication_asks_for_the_patients_details(self):
        text = GLP1WeightLossAppeal.static_appeal()
        self.assertNotIn("FDA-approved for the patient's condition", text)
        self.assertIn("[Specify the patient's BMI and weight-related comorbidity]", text)


class TestFillInsAreCaughtBeforeALetterLeaves(unittest.TestCase):
    """Every fill-in a template adds must be one the print and fax blank
    check and the assistant's placeholder list both find, or a letter could
    go out with the instruction still in it."""

    FILL_INS = (
        (
            AdvancedImagingAppeal,
            "[Specify the ACR Appropriateness Criteria scenario that rates this "
            "study usually appropriate, and why a lower-cost study was not "
            "enough, or delete this sentence]",
        ),
        (
            AdvancedImagingAppeal,
            "[Specify the NCD and the covered indication that matches this "
            "order, or delete this sentence]",
        ),
        (
            SpecialtyMedicationAppeal,
            "[Specify which applies and list the agents tried, with dates]",
        ),
        (
            GLP1WeightLossAppeal,
            "[Specify the patient's BMI and weight-related comorbidity]",
        ),
        (
            PostSurgicalRehabAppeal,
            "[Specify the surgery and its date if this care follows surgery, "
            "or delete this sentence]",
        ),
    )

    def test_each_fill_in_is_in_its_letter(self):
        for template, fill_in in self.FILL_INS:
            with self.subTest(fill_in=fill_in):
                self.assertIn(fill_in, template.static_appeal())

    def test_the_blank_check_finds_each_fill_in(self):
        from fighthealthinsurance.letter_placeholders import (
            find_placeholder_spans,
            find_unfilled_placeholders,
        )

        for template, fill_in in self.FILL_INS:
            with self.subTest(fill_in=fill_in):
                letter = template.static_appeal()
                found = [letter[a:b] for a, b in find_placeholder_spans(letter)]
                self.assertIn(fill_in, found)
                self.assertIn(fill_in, find_unfilled_placeholders(letter))

    def test_the_assistant_lists_each_fill_in(self):
        from fighthealthinsurance.assistant_drafts import placeholders_in

        for template, fill_in in self.FILL_INS:
            with self.subTest(fill_in=fill_in):
                self.assertIn(fill_in, placeholders_in(template.static_appeal()))

    def test_no_bracket_the_checks_miss(self):
        from fighthealthinsurance.letter_placeholders import find_placeholder_spans

        for template in SPECIALIZED_DENIAL_TEMPLATES:
            letter = template.static_appeal()
            found = {letter[a:b] for a, b in find_placeholder_spans(letter)}
            for bracket in re.findall(r"\[[^\[\]\n]+\]", letter):
                with self.subTest(template=template.__name__, bracket=bracket):
                    self.assertIn(bracket, found)


class TestOlderTemplatesAskRatherThanAssert(unittest.TestCase):
    def test_no_claim_that_the_denial_skipped_its_analysis(self):
        for template in SPECIALIZED_DENIAL_TEMPLATES:
            with self.subTest(template=template.__name__):
                letter = template.static_appeal()
                self.assertNotIn("does not engage with", letter)

    def test_glp1_does_not_declare_the_drug_medically_necessary(self):
        self.assertNotIn(
            "covered medical condition and is medically necessary",
            GLP1WeightLossAppeal.static_appeal(),
        )

    def test_therapy_letter_does_not_assume_a_cap(self):
        letter = PhysicalTherapyContinuationAppeal.static_appeal()
        self.assertNotIn("The denial appears to rest on", letter)
        self.assertIn("If the denial rests on a visit cap", letter)

    def test_rehab_letter_does_not_assume_surgery(self):
        letter = PostSurgicalRehabAppeal.static_appeal()
        self.assertNotIn("following surgery for", letter)
        self.assertNotIn("post-operative status", letter)


class TestSynthesisKeepsFillIns(unittest.TestCase):
    def test_synthesis_prompt_keeps_bracketed_fill_ins(self):
        prompt = AppealGenerator.SYNTHESIS_SYSTEM_PROMPT
        self.assertIn("Keep bracketed fill-ins", prompt)
        self.assertIn("never turn them into statements of fact", prompt)


class TestRealImagingHintBlock(unittest.TestCase):
    def test_hint_block_carries_the_imaging_guardrails(self):
        block = AppealGenerator._build_specialized_hint_block([AdvancedImagingAppeal])
        self.assertIn("Medicare plans only", block)
        self.assertIn("cite a specific NCD only if one fits", block)
        self.assertIn("Do not state that the study meets ACR criteria", block)
        self.assertNotIn("satisfies the relevant NCD", block)


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
        self.assertIn("Medicare Advantage", text)


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
        self.assertIn("Medicare Advantage", text)


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

    def test_does_not_match_bare_acl_repair_denial(self):
        # Denial of the surgery itself (with no rehab context) must not
        # be classified as a post-surgical rehab denial.
        self.assertFalse(
            PostSurgicalRehabAppeal.matches(
                "ACL repair is not medically necessary.",
                procedure="ACL reconstruction",
            )
        )

    def test_does_not_match_bare_rotator_cuff_denial(self):
        self.assertFalse(
            PostSurgicalRehabAppeal.matches(
                "Rotator cuff repair surgery is denied.",
            )
        )

    def test_matches_post_surgical_therapy_phrase(self):
        self.assertTrue(
            PostSurgicalRehabAppeal.matches(
                "Post-operative physical therapy following ACL "
                "reconstruction is denied.",
            )
        )

    def test_static_appeal_cites_cms_chapters(self):
        text = PostSurgicalRehabAppeal.static_appeal()
        self.assertIn("Medicare Benefit Policy Manual", text)
        self.assertIn("Jimmo", text)
        self.assertIn("Medicare Advantage", text)


class TestGLP1WeightLossAppealMatches(unittest.TestCase):
    def test_matches_zepbound_text(self):
        self.assertTrue(
            GLP1WeightLossAppeal.matches(
                "Coverage for Zepbound is denied.",
            )
        )

    def test_matches_anti_obesity_medication(self):
        self.assertTrue(
            GLP1WeightLossAppeal.matches(
                "Anti-obesity medication is excluded under your plan.",
            )
        )

    def test_matches_wegovy_with_weight_context(self):
        self.assertTrue(
            GLP1WeightLossAppeal.matches(
                "Wegovy for weight loss is denied as not medically necessary.",
            )
        )

    def test_does_not_match_obesity_diagnosis_without_medication(self):
        # An obesity/overweight diagnosis alone must NOT fire the medication
        # template — e.g. bariatric surgery or nutrition-counseling denials
        # that merely carry an obesity diagnosis.
        self.assertFalse(
            GLP1WeightLossAppeal.matches(
                "Bariatric surgery is denied as not medically necessary.",
                procedure="Bariatric surgery",
                diagnosis="Obesity",
            )
        )

    def test_does_not_match_diabetes_ozempic_denial(self):
        # Diabetes GLP-1 denials are handled by SpecialtyMedicationAppeal;
        # this weight-management template should not fire on a bare
        # Ozempic-for-diabetes denial with no weight/obesity context.
        self.assertFalse(
            GLP1WeightLossAppeal.matches(
                "Ozempic is denied; patient must try metformin first.",
                diagnosis="Type 2 diabetes",
            )
        )

    def test_static_appeal_cites_obesity_disease_and_fda(self):
        text = GLP1WeightLossAppeal.static_appeal()
        self.assertIn("H-440.842", text)
        self.assertIn("42 U.S.C. § 18116", text)
        self.assertIn("Medicare Advantage", text)

    def test_static_appeal_uses_substitution_placeholders(self):
        text = GLP1WeightLossAppeal.static_appeal()
        self.assertIn("{insurance_company}", text)
        self.assertIn("{claim_id}", text)
        self.assertIn("{procedure}", text)
        self.assertIn("{diagnosis}", text)


class TestAlgorithmGuidanceIsMedicareAdvantageOnly(unittest.TestCase):
    """CMS's algorithm guidance (CMS-4201-F and its February 6, 2024 memo)
    binds Medicare Advantage plans only, so no template presents it as a
    rule for every plan, and each letter grounds the request for other plans
    in the claims rule."""

    def test_no_template_cites_a_cms_2024_algorithm_rule(self):
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            for text in (cls.static_appeal(),) + cls.citations:
                with self.subTest(template=cls.__name__, text=text[:60]):
                    self.assertNotIn("CMS 2024 Final Rule", text)

    def test_algorithm_citations_are_marked_medicare_advantage_only(self):
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            for citation in cls.citations:
                if re.search(r"algorithm|artificial intelligence", citation, re.I):
                    with self.subTest(template=cls.__name__, text=citation[:60]):
                        self.assertTrue(
                            citation.startswith("Medicare Advantage plans only")
                        )

    def test_letters_tie_cms_algorithm_guidance_to_medicare_advantage(self):
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            for sentence in re.split(r"(?<=\.)\s", cls.static_appeal()):
                if "CMS" in sentence and "algorithm" in sentence:
                    with self.subTest(template=cls.__name__, text=sentence[:60]):
                        self.assertIn("Medicare Advantage", sentence)

    def test_letters_ground_the_tool_request_in_the_claims_rule(self):
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            text = cls.static_appeal()
            with self.subTest(template=cls.__name__):
                self.assertRegex(text, r"(?i)automated (?:review )?tool")
                self.assertIn("29 CFR 2560.503-1", text)

    def test_clinical_judgment_duty_is_tied_to_medical_necessity_denials(self):
        # 29 CFR 2560.503-1(g)(1)(v)(B) reaches only those denials.
        checked = 0
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            for sentence in re.split(r"(?<=\.)\s", cls.static_appeal()):
                if "clinical judgment" in sentence:
                    checked += 1
                    with self.subTest(template=cls.__name__, text=sentence[:60]):
                        self.assertIn("based on medical necessity", sentence)
        self.assertGreaterEqual(checked, len(SPECIALIZED_DENIAL_TEMPLATES))

    def test_medicare_advantage_citations_exclude_part_d_drugs(self):
        checked = 0
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            for citation in cls.citations:
                if citation.startswith("Medicare Advantage plans only"):
                    checked += 1
                    with self.subTest(template=cls.__name__):
                        self.assertIn(
                            "for Part A and Part B benefits, not Part D", citation
                        )
        self.assertGreater(checked, 0)

    def test_drug_letters_apply_part_422_only_to_part_b_drugs(self):
        # Part 422 and the CMS memo cover Part A and B benefits only.
        for cls in (
            MentalHealthParityAppeal,
            SpecialtyMedicationAppeal,
            GLP1WeightLossAppeal,
        ):
            text = cls.static_appeal()
            with self.subTest(template=cls.__name__):
                self.assertIn("42 CFR Part 423, Subpart M", text)
            for sentence in re.split(r"(?<=\.)\s", text):
                if "422.101" in sentence:
                    with self.subTest(template=cls.__name__, text=sentence[:60]):
                        self.assertIn("Part B drug", sentence)


class TestMatchingFlags(unittest.TestCase):
    """`matches()` uses IGNORECASE and DOTALL so that real-world denial
    letters (which routinely break key phrases across newlines) still
    fire the appropriate template, and so that exclusion patterns can
    block matches that would otherwise be triggered by procedure or
    diagnosis fields.
    """

    def test_dotall_lets_post_surgical_rehab_span_newlines(self):
        # Without DOTALL, `\bpost[\s\-]?(surgical|operative|op)\b.*\b(rehab|...)`
        # would not match this letter because the keywords are on
        # separate lines.
        self.assertTrue(
            PostSurgicalRehabAppeal.matches(
                "We have reviewed your request for post-surgical care.\n"
                "After review, the requested rehab is not approved.",
            )
        )

    def test_negative_pattern_blocks_match_via_procedure_field(self):
        # The PT continuation template excludes "physician assistant" so
        # that an unrelated denial about a PA-led visit doesn't get
        # misclassified. The exclusion must apply to the procedure
        # field, not just the denial text.
        self.assertFalse(
            PhysicalTherapyContinuationAppeal.matches(
                "Maximum number of visits reached.",
                procedure="Physician assistant follow-up",
            )
        )


class TestNoUnresolvedMedicalReasonInStaticAppeals(unittest.TestCase):
    """Specialized templates feed `non_ai_appeals` directly; sub_in_appeals
    only handles {insurance_company}, {claim_id}, {procedure}, and
    {diagnosis}. Any leftover {medical_reason} placeholder would surface
    verbatim in the user's appeal letter.
    """

    def test_no_medical_reason_placeholder_in_any_static_appeal(self):
        for cls in SPECIALIZED_DENIAL_TEMPLATES:
            with self.subTest(template=cls.__name__):
                self.assertNotIn("{medical_reason}", cls.static_appeal())


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

    def test_detect_picks_glp1_weight_loss(self):
        matches = detect_specialized_templates(
            "Zepbound for chronic weight management is denied.",
        )
        self.assertIn(GLP1WeightLossAppeal, matches)

    def test_detect_returns_empty_for_non_matching_denial(self):
        matches = detect_specialized_templates(
            "Routine annual checkup paid in full; no denial.",
        )
        self.assertEqual(matches, [])

    def test_specialized_registry_contains_all_templates(self):
        names = {t.__name__ for t in SPECIALIZED_DENIAL_TEMPLATES}
        self.assertEqual(
            names,
            {
                "MentalHealthParityAppeal",
                "AdvancedImagingAppeal",
                "SpecialtyMedicationAppeal",
                "PhysicalTherapyContinuationAppeal",
                "PostSurgicalRehabAppeal",
                "GLP1WeightLossAppeal",
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
            # The completion-ordered fan-in, stubbed so make_appeals runs
            # with no model results.
            "fighthealthinsurance.generate_appeal._appeals_as_futures_complete",
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

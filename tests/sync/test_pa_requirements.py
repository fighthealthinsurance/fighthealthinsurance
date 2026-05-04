"""Tests for the payer prior-auth requirement lookup helpers."""

from datetime import date

from django.test import TestCase

from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    PayerPriorAuthRequirement,
)
from fighthealthinsurance.pa_requirements import (
    extract_cpt_hcpcs_codes,
    format_pa_context,
    generate_pa_questions,
    get_pa_context_for_denial,
    get_pa_questions_for_denial,
    infer_line_of_business,
    lookup_pa_requirements,
)


class CodeExtractionTests(TestCase):
    """Verify CPT and HCPCS Level II code extraction from free-form text."""

    def test_extracts_cpt_codes(self):
        text = "Patient received polysomnography (CPT 95810) on 2025-03-04."
        self.assertEqual(extract_cpt_hcpcs_codes(text), ["95810"])

    def test_extracts_hcpcs_codes(self):
        text = "Belimumab J0490 was administered IV per UHC drug policy."
        self.assertEqual(extract_cpt_hcpcs_codes(text), ["J0490"])

    def test_extracts_mixed_codes_dedup_and_order(self):
        text = "Codes 95810 and J0490 and J0490 again, then 95810."
        self.assertEqual(extract_cpt_hcpcs_codes(text), ["95810", "J0490"])

    def test_ignores_diagnosis_codes(self):
        # Z51.11 and W50.0XXA both fall outside the HCPCS prefix subset.
        text = "Diagnosis Z51.11 and W50.0XXA — no procedures listed."
        self.assertEqual(extract_cpt_hcpcs_codes(text), [])

    def test_excludes_dotted_icd10_in_overlap_letters(self):
        # J45.20 (asthma) is in the J range that HCPCS uses for drug codes;
        # the dotted form must be stripped before HCPCS scanning.
        text = "Patient with asthma J45.20 received bevacizumab J9035."
        self.assertEqual(extract_cpt_hcpcs_codes(text), ["J9035"])

    def test_excludes_compact_icd10_when_dotted_form_appears(self):
        # If the same diagnosis is written both with and without a period
        # (e.g. OCR variants), the compact form should also be excluded.
        text = "Diagnosis M54.50 (recurrent: M5450). No procedures billed."
        self.assertEqual(extract_cpt_hcpcs_codes(text), [])

    def test_excludes_retired_or_unused_hcpcs_letters(self):
        # M-prefix HCPCS codes are mostly retired; we exclude them to avoid
        # false positives on OCR'd ICD-10 musculoskeletal codes.
        text = "Codes M0064 and N1234 should not be treated as HCPCS here."
        self.assertEqual(extract_cpt_hcpcs_codes(text), [])

    def test_handles_modifiers(self):
        # Code with -26 modifier; we capture only the underlying code.
        text = "Bill 95810-26 for the professional component."
        codes = extract_cpt_hcpcs_codes(text)
        self.assertIn("95810", codes)

    def test_handles_empty_text(self):
        self.assertEqual(extract_cpt_hcpcs_codes(""), [])
        self.assertEqual(extract_cpt_hcpcs_codes(None), [])


class LineOfBusinessInferenceTests(TestCase):
    """Verify denial-text → LOB inference."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )

    def _denial(self, **kwargs):
        defaults = {
            "hashed_email": "x" * 64,
            "denial_text": "",
        }
        defaults.update(kwargs)
        return Denial.objects.create(**defaults)

    def test_detects_medicare_advantage(self):
        d = self._denial(
            denial_text="UnitedHealthcare Medicare Advantage HMO denied your request."
        )
        self.assertEqual(infer_line_of_business(d), "medicare_advantage")

    def test_detects_medicaid(self):
        d = self._denial(
            denial_text="Medicaid Community Plan denial — coverage not approved."
        )
        self.assertEqual(infer_line_of_business(d), "medicaid")

    def test_detects_dsnp_takes_precedence(self):
        d = self._denial(
            denial_text="UnitedHealthcare Dual Special Needs Plan (DSNP) member services."
        )
        self.assertEqual(infer_line_of_business(d), "dsnp")

    def test_returns_none_when_unknown(self):
        d = self._denial(denial_text="Coverage denied for billing reasons.")
        self.assertIsNone(infer_line_of_business(d))


class PaRequirementLookupTests(TestCase):
    """Verify lookup_pa_requirements filters and date logic."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna",
            regex=r"aetna",
            negative_regex=r"$^",
        )

        # Active national rule (no state, all LOBs).
        self.req_psg = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="95810",
            code_description="Polysomnography",
            pa_category="Sleep Medicine",
            criteria_reference="UHC Sleep Medicine policy",
        )

        # MA-only rule for a J-code.
        self.req_belimumab = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="J0490",
            line_of_business="medicare_advantage",
            code_description="Belimumab",
            criteria_reference="UHC MBD policy: Belimumab",
        )

        # Range rule.
        self.req_spine = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            code_range_start="22510",
            code_range_end="22515",
            code_description="Vertebroplasty range",
        )

        # Negative rule (not required).
        self.req_office_visit = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="99213",
            requires_pa=False,
            notes="Office visit, not subject to PA",
        )

        # Expired rule.
        self.req_expired = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="00001",
            effective_date=date(2020, 1, 1),
            end_date=date(2021, 1, 1),
        )

    def test_lookup_by_exact_code(self):
        results = lookup_pa_requirements(["95810"], insurance_company=self.uhc)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].pk, self.req_psg.pk)

    def test_lookup_by_range(self):
        results = lookup_pa_requirements(["22513"], insurance_company=self.uhc)
        self.assertEqual([r.pk for r in results], [self.req_spine.pk])

    def test_lookup_filters_by_lob(self):
        # commercial LOB should NOT pull the MA-only belimumab rule.
        results = lookup_pa_requirements(
            ["J0490"], insurance_company=self.uhc, line_of_business="commercial"
        )
        self.assertEqual(results, [])

        # medicare_advantage LOB SHOULD pull it.
        results = lookup_pa_requirements(
            ["J0490"],
            insurance_company=self.uhc,
            line_of_business="medicare_advantage",
        )
        self.assertEqual([r.pk for r in results], [self.req_belimumab.pk])

    def test_lookup_filters_by_payer(self):
        results = lookup_pa_requirements(["95810"], insurance_company=self.aetna)
        self.assertEqual(results, [])

    def test_lookup_excludes_expired_rules(self):
        results = lookup_pa_requirements(
            ["00001"], insurance_company=self.uhc, on_date=date.today()
        )
        self.assertEqual(results, [])

        # A date inside the validity window finds it.
        results = lookup_pa_requirements(
            ["00001"], insurance_company=self.uhc, on_date=date(2020, 6, 1)
        )
        self.assertEqual([r.pk for r in results], [self.req_expired.pk])

    def test_lookup_with_no_codes_returns_empty(self):
        self.assertEqual(lookup_pa_requirements([], insurance_company=self.uhc), [])
        self.assertEqual(lookup_pa_requirements([""], insurance_company=self.uhc), [])

    def test_lookup_negative_rule_returned(self):
        results = lookup_pa_requirements(["99213"], insurance_company=self.uhc)
        self.assertEqual([r.pk for r in results], [self.req_office_visit.pk])
        self.assertFalse(results[0].requires_pa)

    def test_unknown_lob_narrows_to_all_lob_only(self):
        # The MA-only J0490 rule must not surface when the caller can't
        # tell which line of business the denial belongs to.
        results = lookup_pa_requirements(
            ["J0490"], insurance_company=self.uhc, line_of_business=None
        )
        self.assertEqual(results, [])

    def test_unknown_state_narrows_to_national_only(self):
        from fighthealthinsurance.models import PayerPriorAuthRequirement

        ny_only = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="33333",
            state="NY",
        )
        # Unknown state filter should not pull the NY-scoped row.
        results = lookup_pa_requirements(
            ["33333"], insurance_company=self.uhc, state=None
        )
        self.assertEqual(results, [])
        # CA caller likewise doesn't see the NY rule, only national rules.
        results = lookup_pa_requirements(
            ["33333"], insurance_company=self.uhc, state="CA"
        )
        self.assertEqual(results, [])
        # NY caller does see it.
        results = lookup_pa_requirements(
            ["33333"], insurance_company=self.uhc, state="NY"
        )
        self.assertEqual([r.pk for r in results], [ny_only.pk])

    def test_unknown_plan_narrows_to_plan_agnostic_only(self):
        from fighthealthinsurance.models import (
            InsurancePlan,
            PayerPriorAuthRequirement,
        )

        gold_plan = InsurancePlan.objects.create(
            insurance_company=self.uhc,
            plan_name="Gold PPO",
        )
        plan_only = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            plan=gold_plan,
            cpt_hcpcs_code="44444",
        )
        # No plan in the lookup → only plan-agnostic rules.
        results = lookup_pa_requirements(["44444"], insurance_company=self.uhc)
        self.assertEqual(results, [])
        # Matching plan → returns the plan-scoped rule.
        results = lookup_pa_requirements(
            ["44444"], insurance_company=self.uhc, plan=gold_plan
        )
        self.assertEqual([r.pk for r in results], [plan_only.pk])


class PayerPriorAuthRequirementCleanTests(TestCase):
    """Verify the model rejects rows whose plan and payer disagree."""

    def setUp(self):
        from fighthealthinsurance.models import InsurancePlan

        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna",
            regex=r"aetna",
            negative_regex=r"$^",
        )
        self.aetna_plan = InsurancePlan.objects.create(
            insurance_company=self.aetna,
            plan_name="Aetna PPO",
        )
        self.uhc_plan = InsurancePlan.objects.create(
            insurance_company=self.uhc,
            plan_name="UHC HMO",
        )

    def test_save_rejects_plan_payer_mismatch(self):
        from django.core.exceptions import ValidationError

        from fighthealthinsurance.models import PayerPriorAuthRequirement

        with self.assertRaises(ValidationError) as ctx:
            PayerPriorAuthRequirement.objects.create(
                insurance_company=self.uhc,
                plan=self.aetna_plan,
                cpt_hcpcs_code="55555",
            )
        self.assertIn("plan", ctx.exception.message_dict)

    def test_save_accepts_matching_plan_and_payer(self):
        from fighthealthinsurance.models import PayerPriorAuthRequirement

        req = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            plan=self.uhc_plan,
            cpt_hcpcs_code="55555",
        )
        self.assertEqual(req.plan_id, self.uhc_plan.pk)


class FormatPaContextTests(TestCase):
    """Verify the prompt block we hand to the LLM."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )
        self.req = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="95810",
            code_description="Polysomnography",
            pa_category="Sleep Medicine",
            criteria_reference="UHC Sleep Medicine policy",
            submission_channel="UHCprovider.com / 866-889-8054",
            source_document="UHC PA list",
            source_document_date=date(2025, 1, 1),
        )
        self.negative = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="99213",
            requires_pa=False,
            notes="Office visit, not subject to PA.",
        )

    def test_format_includes_payer_code_and_criteria(self):
        block = format_pa_context([self.req])
        self.assertIn("UnitedHealthcare", block)
        self.assertIn("95810", block)
        self.assertIn("REQUIRES", block)
        self.assertIn("UHC Sleep Medicine policy", block)
        self.assertIn("UHCprovider.com / 866-889-8054", block)
        self.assertIn("UHC PA list", block)

    def test_format_marks_negative_rules_explicitly(self):
        block = format_pa_context([self.negative])
        self.assertIn("does NOT require", block)

    def test_format_notes_unmatched_codes(self):
        block = format_pa_context([self.req], requested_codes=["95810", "99999"])
        self.assertIn("95810", block)
        self.assertIn("99999", block)
        self.assertIn("not in our index", block)

    def test_format_returns_empty_string_when_nothing_useful(self):
        self.assertEqual(format_pa_context([], requested_codes=None), "")
        self.assertEqual(format_pa_context([], requested_codes=[]), "")


class GetPaContextForDenialTests(TestCase):
    """Verify the high-level denial → context entry point."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )
        PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="95810",
            code_description="Polysomnography",
            criteria_reference="UHC Sleep Medicine policy",
        )

    def test_returns_context_when_denial_text_has_code_and_payer(self):
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text=(
                "UnitedHealthcare denied authorization for polysomnography (95810). "
                "The denial cites no medical necessity."
            ),
            insurance_company_obj=self.uhc,
        )
        context = get_pa_context_for_denial(denial)
        self.assertIn("95810", context)
        self.assertIn("UHC Sleep Medicine policy", context)

    def test_returns_empty_string_when_no_codes(self):
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text="Coverage denied without a specific procedure mentioned.",
            insurance_company_obj=self.uhc,
        )
        self.assertEqual(get_pa_context_for_denial(denial), "")

    def test_returns_empty_string_when_payer_unknown(self):
        # When neither insurance_company_obj nor a resolvable text payer is
        # set, refuse the lookup. Surfacing rules from another carrier in
        # payer-attributed appeal context would be misleading.
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text="The insurer denied 95810 polysomnography.",
        )
        self.assertEqual(get_pa_context_for_denial(denial), "")

    def test_resolves_payer_from_text_field_when_fk_missing(self):
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text="UnitedHealthcare denied 95810 polysomnography.",
            insurance_company="UnitedHealthcare",
        )
        context = get_pa_context_for_denial(denial)
        self.assertIn("95810", context)


class MakeOpenPromptIncludesPaContextTests(TestCase):
    """Verify the appeal-generation prompt picks up pa_context."""

    def test_make_open_prompt_includes_pa_context_block(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        prompt = generator.make_open_prompt(
            denial_text="UHC denied 95810.",
            procedure="Polysomnography",
            diagnosis="Suspected OSA",
            insurance_company="UnitedHealthcare",
            pa_context=(
                "Payer prior authorization requirements ...:\n"
                "- UnitedHealthcare: code 95810 REQUIRES prior authorization (LOB=All Lines of Business)."
            ),
        )

        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn("PAYER PRIOR-AUTH RULES", prompt)
        self.assertIn("95810 REQUIRES", prompt)

    def test_make_open_prompt_omits_pa_block_when_empty(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        prompt = generator.make_open_prompt(
            denial_text="UHC denied 95810.",
            insurance_company="UnitedHealthcare",
            pa_context="",
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertNotIn("PAYER PRIOR-AUTH RULES", prompt)


class GeneratePaQuestionsTests(TestCase):
    """Verify PA-rule-derived clarifying questions."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )

    def _make_req(self, **kwargs):
        defaults = {
            "insurance_company": self.uhc,
            "cpt_hcpcs_code": "00000",
            "pa_category": "",
            "criteria_reference": "",
            "submission_channel": "",
            "requires_pa": True,
            "notification_only": False,
        }
        defaults.update(kwargs)
        return PayerPriorAuthRequirement.objects.create(**defaults)

    def test_returns_empty_for_no_requirements(self):
        self.assertEqual(generate_pa_questions([]), [])

    def test_category_questions_for_sleep_medicine(self):
        req = self._make_req(cpt_hcpcs_code="95810", pa_category="Sleep Medicine")
        questions = generate_pa_questions([req])
        self.assertTrue(any("Epworth" in q or "STOP-BANG" in q for q, _ in questions))
        self.assertLessEqual(len(questions), 4)

    def test_category_questions_for_genetic_testing(self):
        req = self._make_req(
            cpt_hcpcs_code="81162",
            pa_category="Genetic and Molecular Testing",
        )
        questions = generate_pa_questions([req])
        self.assertTrue(
            any("genetic counseling" in q.lower() for q, _ in questions),
            f"Expected genetic-counseling question, got: {questions}",
        )

    def test_falls_back_to_criteria_question_for_unknown_category(self):
        req = self._make_req(
            cpt_hcpcs_code="11111",
            pa_category="Unknown Category",
            criteria_reference="UHC Policy: Some Procedure",
        )
        questions = generate_pa_questions([req])
        joined = " ".join(q for q, _ in questions)
        self.assertIn("UHC Policy: Some Procedure", joined)

    def test_negative_rule_generates_billing_question(self):
        req = self._make_req(cpt_hcpcs_code="99213", requires_pa=False)
        questions = generate_pa_questions([req])
        joined = " ".join(q for q, _ in questions).lower()
        self.assertIn("not required", joined)

    def test_notification_only_generates_notification_question(self):
        req = self._make_req(
            cpt_hcpcs_code="22222",
            notification_only=True,
            criteria_reference="UHC Notification: Some Procedure",
        )
        questions = generate_pa_questions([req])
        joined = " ".join(q for q, _ in questions).lower()
        self.assertIn("notif", joined)

    def test_caps_questions_at_max(self):
        # Three category-question categories should still be capped at 2.
        reqs = [
            self._make_req(cpt_hcpcs_code="11111", pa_category="Sleep Medicine"),
            self._make_req(
                cpt_hcpcs_code="22222",
                pa_category="Genetic and Molecular Testing",
            ),
            self._make_req(
                cpt_hcpcs_code="33333",
                pa_category="Advanced Outpatient Imaging",
            ),
        ]
        questions = generate_pa_questions(reqs, max_questions=2)
        self.assertEqual(len(questions), 2)

    def test_dedupes_identical_questions(self):
        req_a = self._make_req(cpt_hcpcs_code="11111", pa_category="Sleep Medicine")
        req_b = self._make_req(cpt_hcpcs_code="22222", pa_category="Sleep Medicine")
        questions = generate_pa_questions([req_a, req_b])
        unique = {q for q, _ in questions}
        self.assertEqual(len(unique), len(questions))


class GetPaQuestionsForDenialTests(TestCase):
    """Verify the denial → PA-question entry point."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            regex=r"united\s*health|uhc",
            negative_regex=r"$^",
        )
        PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="95810",
            pa_category="Sleep Medicine",
            criteria_reference="UHC Sleep Medicine policy",
        )

    def test_returns_questions_when_denial_text_has_code(self):
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text="UHC denied authorization for polysomnography (95810).",
            insurance_company_obj=self.uhc,
        )
        questions = get_pa_questions_for_denial(denial)
        self.assertGreater(len(questions), 0)
        self.assertTrue(
            all(isinstance(q, str) and q.endswith("?") for q, _ in questions)
        )

    def test_returns_empty_when_no_codes_in_denial(self):
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text="Coverage was denied without a procedure code.",
            insurance_company_obj=self.uhc,
        )
        self.assertEqual(get_pa_questions_for_denial(denial), [])

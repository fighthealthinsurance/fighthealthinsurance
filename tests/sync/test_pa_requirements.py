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
    get_pa_context_for_denial,
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
        # ICD-10 codes always start with W/X/Y/Z or have a letter+digit+digit
        # form; the regex restricts HCPCS to A-V so these should not match.
        text = "Diagnosis Z51.11 and W50.0XXA — no procedures listed."
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
        # When insurance_company_obj is None but a code is present, lookup
        # still runs across all payers — verify it finds the seed row.
        denial = Denial.objects.create(
            hashed_email="x" * 64,
            denial_text="The insurer denied 95810 polysomnography.",
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

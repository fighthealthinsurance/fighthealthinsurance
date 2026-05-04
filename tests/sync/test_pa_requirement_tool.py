"""Tests for the PA requirement lookup chat tool."""

import re
from unittest.mock import AsyncMock

from django.test import TestCase

from fighthealthinsurance.chat.tools import (
    LOOKUP_PA_REQUIREMENT_REGEX,
    PaRequirementLookupTool,
)
from fighthealthinsurance.chat.tools.pa_requirement_tool import (
    _resolve_insurance_company,
    _run_lookup,
)
from fighthealthinsurance.models import (
    InsuranceCompany,
    PayerPriorAuthRequirement,
)


class LookupPaRequirementPatternTests(TestCase):
    """Verify the chat-tool regex matches realistic LLM output formats."""

    def test_matches_basic_call(self):
        text = 'lookup_pa_requirement {"codes": ["95810"], "payer": "UHC"}'
        match = re.search(LOOKUP_PA_REQUIREMENT_REGEX, text, re.DOTALL | re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("95810", match.group(1))

    def test_matches_with_double_asterisks(self):
        text = '**lookup_pa_requirement {"codes": ["J0490"], "payer": "UnitedHealthcare", "line_of_business": "medicare_advantage"}**'
        match = re.search(LOOKUP_PA_REQUIREMENT_REGEX, text, re.DOTALL | re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("J0490", match.group(1))

    def test_does_not_match_unrelated_text(self):
        text = "I will look up the PA requirements for code 95810 next."
        match = re.search(LOOKUP_PA_REQUIREMENT_REGEX, text, re.DOTALL | re.IGNORECASE)
        self.assertIsNone(match)


class ResolveInsuranceCompanyTests(TestCase):
    """Verify payer name → InsuranceCompany resolution."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            alt_names="UHC\nUnited Healthcare\nUnitedHealth",
            regex=r"united\s*health\s*care|uhc",
            negative_regex=r"$^",
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna",
            alt_names="Aetna Inc",
            regex=r"aetna",
            negative_regex=r"$^",
        )

    def test_exact_name_match(self):
        self.assertEqual(_resolve_insurance_company("UnitedHealthcare"), self.uhc)

    def test_alt_name_match(self):
        self.assertEqual(_resolve_insurance_company("UHC"), self.uhc)
        self.assertEqual(_resolve_insurance_company("United Healthcare"), self.uhc)

    def test_unknown_payer_returns_none(self):
        self.assertIsNone(_resolve_insurance_company("Bogus Insurance Co"))

    def test_empty_payer_returns_none(self):
        self.assertIsNone(_resolve_insurance_company(""))
        self.assertIsNone(_resolve_insurance_company(None))


class RunLookupTests(TestCase):
    """Verify the synchronous core that powers the chat tool."""

    def setUp(self):
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            alt_names="UHC",
            regex=r"united\s*health\s*care|uhc",
            negative_regex=r"$^",
        )
        PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="95810",
            code_description="Polysomnography",
            criteria_reference="UHC Sleep Medicine policy",
            submission_channel="UHCprovider.com",
        )

    def test_lookup_returns_block_and_summary(self):
        block, summary = _run_lookup(
            {"codes": ["95810"], "payer": "UHC", "line_of_business": "commercial"}
        )
        self.assertIn("95810", block)
        self.assertIn("UHCprovider.com", block)
        self.assertIn("UnitedHealthcare", summary)
        self.assertIn("1 PA requirement", summary)

    def test_lookup_accepts_string_codes(self):
        block, _ = _run_lookup({"codes": "95810", "payer": "UHC"})
        self.assertIn("95810", block)

    def test_lookup_handles_missing_codes(self):
        block, summary = _run_lookup({"payer": "UHC"})
        self.assertEqual(block, "")
        self.assertIn("No valid CPT/HCPCS codes", summary)

    def test_lookup_rejects_arbitrary_text_as_code(self):
        # The string-codes fallback should only honor inputs that actually
        # look like CPT (5 digits) or HCPCS Level II (letter + 4 digits).
        block, summary = _run_lookup({"codes": "please lookup my code", "payer": "UHC"})
        self.assertEqual(block, "")
        self.assertIn("No valid CPT/HCPCS codes", summary)

    def test_lookup_with_unknown_payer_refuses_to_broaden(self):
        # An unresolved payer must NOT silently broaden to a cross-payer
        # search — that would surface another carrier's rule as if it
        # applied to the user's insurer. The lookup short-circuits and
        # tells the caller why.
        block, summary = _run_lookup({"codes": ["95810"], "payer": "Unknown Plan"})
        self.assertEqual(block, "")
        self.assertIn("Could not resolve", summary)
        self.assertIn("Unknown Plan", summary)


class PaRequirementLookupToolTests(TestCase):
    """Verify the tool object detects and ignores patterns correctly."""

    def test_detects_tool_call(self):
        tool = PaRequirementLookupTool(AsyncMock())
        text = '**lookup_pa_requirement {"codes": ["95810"], "payer": "UHC"}**'
        match = tool.detect(text)
        self.assertIsNotNone(match)

    def test_ignores_unrelated_text(self):
        tool = PaRequirementLookupTool(AsyncMock())
        match = tool.detect("Just a regular message about prior auth")
        self.assertIsNone(match)

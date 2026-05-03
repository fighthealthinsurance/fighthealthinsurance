"""Test UCR narrative injection into the appeal generation prompt.

Verifies that AppealGenerator.make_open_prompt threads the ucr_context block
into the prompt body when present, and is a no-op when absent.
"""

from django.test import TestCase

from fighthealthinsurance.generate_appeal import AppealGenerator


class UCRPromptInjectionTests(TestCase):
    def setUp(self):
        self.gen = AppealGenerator()

    def test_ucr_block_appears_when_provided(self):
        narrative = (
            "[UCR PRICING CONTEXT]\n"
            "Procedure: 99213\n"
            "Insurer allowed: $80.00\n"
            "Independent benchmark (medicare_pfs):\n"
            "  - p80: $196.84 (derived)\n"
            "Gap vs. proxy-p80 benchmark: $116.84 (59%) under-reimbursed\n"
            "[/UCR PRICING CONTEXT]"
        )
        prompt = self.gen.make_open_prompt(
            denial_text="The claim was denied as out of network reimbursable at UCR.",
            procedure="Office visit",
            diagnosis="Headache",
            ucr_context=narrative,
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None  # for mypy / type narrowing
        self.assertIn("UCR PRICING INSTRUCTIONS", prompt)
        self.assertIn("[UCR PRICING CONTEXT]", prompt)
        self.assertIn("$116.84", prompt)
        self.assertIn("p80: $196.84", prompt)

    def test_no_ucr_block_when_absent(self):
        prompt = self.gen.make_open_prompt(
            denial_text="The claim was denied as not medically necessary.",
            procedure="MRI",
            diagnosis="Back pain",
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertNotIn("UCR PRICING", prompt)
        self.assertNotIn("[UCR PRICING CONTEXT]", prompt)

    def test_empty_ucr_string_is_treated_as_absent(self):
        prompt = self.gen.make_open_prompt(
            denial_text="Denial body",
            procedure="X",
            diagnosis="Y",
            ucr_context="",
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertNotIn("UCR PRICING", prompt)

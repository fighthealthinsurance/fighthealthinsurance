import unittest

from fighthealthinsurance.ml.bad_output_utils import (
    BAD_OUTPUT_PHRASES,
    BAD_OUTPUT_PHRASES_LOWER,
    is_bad_output,
)


class TestBadOutputUtils(unittest.TestCase):
    def test_shared_fixture_cases_guardrail_phrases(self):
        fixture_cases = [
            "As an AI, I do not have the capability to submit this appeal.",
            "I cannot directly create legal or medical documents for you.",
            "Unfortunately, I cannot directly generate this content.",
            "The independent medical review found that this claim should be denied.",
        ]
        for case in fixture_cases:
            with self.subTest(case=case):
                self.assertTrue(is_bad_output(case, check_guardrail_phrases=True))

    def test_baseline_behavior_preserved_without_guardrail_checks(self):
        self.assertTrue(is_bad_output(None))
        self.assertTrue(is_bad_output("  "))
        self.assertTrue(is_bad_output("ok"))
        self.assertFalse(is_bad_output("This is valid text."))

    def test_canonical_phrase_list_contains_expected_items(self):
        self.assertIn("As an AI, I do not have the capability", BAD_OUTPUT_PHRASES)
        self.assertIn(
            "I am an AI assistant and do not have the authority to create medical documents",
            BAD_OUTPUT_PHRASES,
        )
        self.assertEqual(
            tuple(phrase.lower() for phrase in BAD_OUTPUT_PHRASES),
            BAD_OUTPUT_PHRASES_LOWER,
        )

    def test_political_phrases_rejected(self):
        political_cases = [
            "The Newly Elected Government is Exploiting the Economic Downturn and causing harm.",
            "The Government's Spending Policies Are Exacerbating the Downturn significantly.",
            "The Government Needs to Increase Its Spending to Stimulate the Economy now.",
        ]
        for case in political_cases:
            with self.subTest(case=case):
                self.assertTrue(is_bad_output(case, check_guardrail_phrases=True))

    def test_repetition_checker_hook(self):
        calls = []

        def checker(text: str) -> bool:
            calls.append(text)
            return True

        result = is_bad_output(
            "This is long enough to pass length checks.",
            check_severe_repetition=True,
            repetition_checker=checker,
        )

        self.assertTrue(result)
        self.assertEqual(calls, ["This is long enough to pass length checks."])

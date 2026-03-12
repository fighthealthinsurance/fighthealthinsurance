from pathlib import Path
import unittest

from fighthealthinsurance.ml.bad_output_utils import BAD_OUTPUT_PHRASES, is_bad_output


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
        self.assertIn(
            "As an AI, I do not have the capability",
            BAD_OUTPUT_PHRASES,
        )
        self.assertIn(
            "I am an AI assistant and do not have the authority to create medical documents",
            BAD_OUTPUT_PHRASES,
        )

    def test_ml_models_bad_result_paths_use_shared_evaluator(self):
        src = Path("fighthealthinsurance/ml/ml_models.py").read_text()
        self.assertGreaterEqual(src.count("return is_bad_output("), 3)

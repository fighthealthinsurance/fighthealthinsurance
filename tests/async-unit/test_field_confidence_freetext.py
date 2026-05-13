"""Tests for ``score_freetext_extraction`` in field_confidence."""

import unittest

from fighthealthinsurance.field_confidence import score_freetext_extraction

SOURCE = "Patient was denied for lumbar MRI as not medically necessary."


class TestScoreFreetextExtraction(unittest.TestCase):
    def test_empty_value_is_low(self):
        self.assertEqual(score_freetext_extraction("", SOURCE), "low")
        self.assertEqual(score_freetext_extraction(None, SOURCE), "low")
        self.assertEqual(score_freetext_extraction("   ", SOURCE), "low")

    def test_exact_false_is_low(self):
        self.assertEqual(score_freetext_extraction("false", SOURCE), "low")
        self.assertEqual(score_freetext_extraction("FALSE", SOURCE), "low")

    def test_unknown_substring_is_low(self):
        self.assertEqual(score_freetext_extraction("unknown", SOURCE), "low")
        self.assertEqual(score_freetext_extraction("Unknown procedure", SOURCE), "low")
        self.assertEqual(score_freetext_extraction("UNKNOWN", SOURCE), "low")

    def test_imr_phrase_is_low(self):
        self.assertEqual(
            score_freetext_extraction("independent medical review", SOURCE),
            "low",
        )
        self.assertEqual(
            score_freetext_extraction(
                "The Independent Medical Review board denied this", SOURCE
            ),
            "low",
        )

    def test_verbatim_match_is_high(self):
        self.assertEqual(score_freetext_extraction("lumbar MRI", SOURCE), "high")

    def test_case_insensitive_match_is_high(self):
        self.assertEqual(score_freetext_extraction("LUMBAR MRI", SOURCE), "high")
        self.assertEqual(score_freetext_extraction("Lumbar Mri", SOURCE), "high")

    def test_plausible_but_unattested_is_medium(self):
        # Not in source but not junk-flagged.
        self.assertEqual(score_freetext_extraction("cervical MRI", SOURCE), "medium")

    def test_whitespace_stripped_before_match(self):
        self.assertEqual(score_freetext_extraction("  lumbar MRI  ", SOURCE), "high")


if __name__ == "__main__":
    unittest.main()

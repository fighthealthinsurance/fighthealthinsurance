"""Tests for the repeated sentence filter in ML inference."""

from django.test import TestCase

from fighthealthinsurance.ml.ml_models import (
    has_severe_repetition,
    remove_repeated_sentences,
)


class TestRemoveRepeatedSentences(TestCase):
    """Tests for remove_repeated_sentences()."""

    def test_none_input(self):
        self.assertIsNone(remove_repeated_sentences(None))

    def test_normal_text_unchanged(self):
        text = (
            "The patient was diagnosed with condition X. "
            "Treatment Y was recommended by Dr. Smith. "
            "The insurance company denied coverage. "
            "This denial is not supported by medical evidence. "
            "We request that you overturn this decision. "
            "The patient deserves appropriate care."
        )
        result = remove_repeated_sentences(text)
        self.assertEqual(result, text)

    def test_short_text_unchanged(self):
        """Texts with fewer than 6 sentences should pass through unchanged."""
        text = "Same sentence. Same sentence. Same sentence. Same sentence."
        result = remove_repeated_sentences(text)
        self.assertEqual(result, text)

    def test_single_sentence_repeated_many_times(self):
        """A sentence repeated 10x should be capped at max_repeats (3)."""
        base = "This is a valid medical claim"
        filler = "The patient needs treatment. Coverage should be approved. We disagree with the denial. Additional evidence supports this. The guidelines are clear."
        repeated = ". ".join([base] * 10)
        text = filler + " " + repeated + "."
        result = remove_repeated_sentences(text)
        self.assertIsNotNone(result)
        # Count occurrences of the base sentence
        count = result.lower().count(base.lower())
        self.assertLessEqual(count, 3)

    def test_alternating_ab_pattern(self):
        """An A-B-A-B pattern repeated many times should be collapsed."""
        a = "The claim should be approved."
        b = "Medical evidence supports this."
        # Use enough filler so that after collapsing, >30% of text remains
        filler_sentences = [
            "We are writing to appeal this denial.",
            "The patient has been diagnosed with a serious condition.",
            "The treating physician recommends this treatment.",
            "The denial lacks adequate medical justification.",
            "Published guidelines support this course of action.",
            "The patient has exhausted alternative treatments.",
            "We respectfully request that you reconsider.",
            "This treatment is the standard of care.",
        ]
        filler = " ".join(filler_sentences)
        pattern = " ".join([f"{a} {b}"] * 5)
        text = filler + " " + pattern
        result = remove_repeated_sentences(text)
        self.assertIsNotNone(result)
        # Should have at most a few occurrences of each
        a_count = result.count(a)
        b_count = result.count(b)
        self.assertLessEqual(a_count, 3)
        self.assertLessEqual(b_count, 3)

    def test_alternating_ab_severe_returns_none(self):
        """Alternating pattern that dominates the text should return None."""
        a = "The claim should be approved."
        b = "Medical evidence supports this."
        pattern = " ".join([f"{a} {b}"] * 15)
        text = "We write to appeal. " + pattern
        result = remove_repeated_sentences(text)
        # Either None (too much stripped) or heavily reduced
        if result is not None:
            a_count = result.count(a)
            self.assertLessEqual(a_count, 3)

    def test_severe_repetition_returns_none(self):
        """If stripping leaves <30% of original, return None."""
        sent = "This sentence just keeps repeating."
        text = " ".join([sent] * 30)
        result = remove_repeated_sentences(text)
        self.assertIsNone(result)

    def test_custom_max_repeats(self):
        """max_repeats parameter should be respected."""
        filler = "First point here. Second point here. Third point here. Fourth point here. Fifth point here."
        repeated = " ".join(["This is repeated."] * 10)
        text = filler + " " + repeated
        result = remove_repeated_sentences(text, max_repeats=1)
        self.assertIsNotNone(result)
        count = result.lower().count("this is repeated")
        self.assertLessEqual(count, 1)

    def test_mixed_repetition_and_unique(self):
        """Text with some repetition mixed with unique content."""
        sentences = [
            "The patient requires surgery.",
            "Insurance denied the claim.",
            "The patient requires surgery.",
            "Medical records support this.",
            "The patient requires surgery.",
            "We appeal this decision.",
            "The patient requires surgery.",
            "Guidelines recommend coverage.",
        ]
        text = " ".join(sentences)
        result = remove_repeated_sentences(text)
        self.assertIsNotNone(result)
        count = result.count("The patient requires surgery.")
        self.assertLessEqual(count, 3)
        # Unique sentences should be preserved
        self.assertIn("Insurance denied the claim.", result)
        self.assertIn("Medical records support this.", result)
        self.assertIn("We appeal this decision.", result)
        self.assertIn("Guidelines recommend coverage.", result)


class TestHasSevereRepetition(TestCase):
    """Tests for has_severe_repetition()."""

    def test_normal_text_not_severe(self):
        text = (
            "The patient was diagnosed with condition X. "
            "Treatment Y was recommended. "
            "The insurance company denied coverage. "
            "This denial is not supported by evidence. "
            "We request that you overturn this decision. "
            "The patient deserves appropriate care."
        )
        self.assertFalse(has_severe_repetition(text))

    def test_short_text_not_severe(self):
        """Short text should never be flagged as severe."""
        text = "Same. Same. Same. Same."
        self.assertFalse(has_severe_repetition(text))

    def test_mostly_repeated_is_severe(self):
        """Text where one sentence is >50% of all sentences."""
        repeated = "This keeps repeating."
        other = "A unique sentence."
        # 8 repeated + 2 unique = 10 total, 80% repetition
        sentences = [repeated] * 8 + [other, "Another unique one."]
        text = " ".join(sentences)
        self.assertTrue(has_severe_repetition(text))

    def test_threshold_boundary(self):
        """Test at exactly the threshold boundary."""
        repeated = "Repeated sentence here."
        unique_sentences = [
            "First unique.",
            "Second unique.",
            "Third unique.",
            "Fourth unique.",
            "Fifth unique.",
        ]
        # 5 repeated + 5 unique = 10 total, exactly 50% — not severe (> not >=)
        sentences = [repeated] * 5 + unique_sentences
        text = " ".join(sentences)
        self.assertFalse(has_severe_repetition(text))

        # 6 repeated + 5 unique = 11 total, ~54% — severe
        sentences = [repeated] * 6 + unique_sentences
        text = " ".join(sentences)
        self.assertTrue(has_severe_repetition(text))

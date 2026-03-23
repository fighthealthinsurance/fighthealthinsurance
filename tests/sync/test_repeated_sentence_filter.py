"""Tests for the repeated sentence filter in ML inference."""

from django.test import TestCase

from fighthealthinsurance.ml.ml_models import (
    has_severe_repetition,
    remove_repeated_blocks,
    remove_repeated_sentences,
    repetition_penalty,
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
        filler = (
            "The patient needs treatment. "
            "Coverage should be approved. "
            "We disagree with the denial. "
            "Additional evidence supports this. "
            "The guidelines are clear."
        )
        repeated = ". ".join([base] * 10)
        text = filler + " " + repeated + "."
        result = remove_repeated_sentences(text)
        self.assertIsNotNone(result)
        count = result.lower().count(base.lower())
        self.assertLessEqual(count, 3)

    def test_alternating_ab_pattern(self):
        """An A-B-A-B pattern repeated many times should have each capped."""
        a = "The claim should be approved."
        b = "Medical evidence supports this."
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
        a_count = result.count(a)
        b_count = result.count(b)
        self.assertLessEqual(a_count, 3)
        self.assertLessEqual(b_count, 3)

    def test_abc_abd_abe_pattern(self):
        """A-B-C, A-B-D, A-B-E etc. — A and B each get capped independently."""
        a = "The treatment is necessary."
        b = "Evidence supports approval."
        unique = [
            "First unique detail here.",
            "Second unique detail here.",
            "Third unique detail here.",
            "Fourth unique detail here.",
            "Fifth unique detail here.",
            "Sixth unique detail here.",
        ]
        # A-B appear 6 times each, interleaved with unique sentences
        sentences = []
        for u in unique:
            sentences.extend([a, b, u])
        text = " ".join(sentences)
        result = remove_repeated_sentences(text)
        self.assertIsNotNone(result)
        a_count = result.count(a)
        b_count = result.count(b)
        self.assertLessEqual(a_count, 3)
        self.assertLessEqual(b_count, 3)
        # All unique sentences should be preserved
        for u in unique:
            self.assertIn(u, result)

    def test_severe_repetition_returns_none(self):
        """If stripping leaves <30% of original, return None."""
        sent = "This sentence just keeps repeating."
        text = " ".join([sent] * 30)
        result = remove_repeated_sentences(text)
        self.assertIsNone(result)

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

    def test_custom_max_repeats(self):
        """max_repeats parameter should be respected."""
        filler = (
            "First point here. "
            "Second point here. "
            "Third point here. "
            "Fourth point here. "
            "Fifth point here."
        )
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

    def test_alternating_pair_is_severe(self):
        """Two sentences alternating A-B-A-B should be detected as severe."""
        a = "The claim is valid."
        b = "Coverage must be provided."
        # 10 alternations of A-B = 20 sentences, all alternating
        pattern = " ".join([f"{a} {b}"] * 10)
        text = pattern
        self.assertTrue(has_severe_repetition(text))

    def test_alternating_pair_with_filler_not_severe(self):
        """Two alternating sentences that don't dominate shouldn't trigger."""
        a = "The claim is valid."
        b = "Coverage must be provided."
        unique_sentences = [
            "First point of evidence.",
            "Second point of evidence.",
            "Third point of evidence.",
            "Fourth point of evidence.",
            "Fifth point of evidence.",
            "Sixth point of evidence.",
            "Seventh point of evidence.",
            "Eighth point of evidence.",
        ]
        # 2 A-B cycles (4 sentences) + 8 unique = 12 total
        # Only 2 distinct sentences = 4/12 = 33%, below threshold
        pattern = f"{a} {b} {a} {b}"
        text = " ".join(unique_sentences) + " " + pattern
        self.assertFalse(has_severe_repetition(text))

    def test_two_clustered_sentences_not_alternating(self):
        """Two sentences clustered (AAABBB) should not trigger alternating detection."""
        a = "The claim is valid."
        b = "Coverage must be provided."
        # All A's then all B's — not alternating even though only 2 distinct
        text = " ".join([a] * 4 + [b] * 4)
        # Single-sentence check: 4/8 = 50%, not > 50%, so not severe by that check.
        # Alternating check: adjacent pairs that differ are only at the A→B boundary (1 out of 7).
        # So this should NOT be flagged as severe.
        self.assertFalse(has_severe_repetition(text))

    def test_repeated_line_block_not_severe(self):
        """Block repetition is handled by remove_repeated_blocks, not here."""
        block = [
            "• Detailed history and examination",
            "• Medical decision-making of moderate complexity",
            "• Counseling and coordination of care",
            "• Services were medically necessary",
        ]
        # Block repeated 5 times — not detected at the sentence level since
        # bullet lines don't end with sentence-ending punctuation.
        text = "\n".join(block * 5)
        self.assertFalse(has_severe_repetition(text))

    def test_repeated_contact_block_not_severe(self):
        """Contact block repetition is handled by remove_repeated_blocks, not here."""
        intro = "Dear Appeals Unit,\nI am writing to appeal.\nPlease review.\n"
        contact = [
            "Phone: {{PHONE_NUMBER}}",
            "Email: {{EMAIL_ADDRESS}}",
            "Fax: {{FAX_NUMBER}}",
            "Mail: {{ADDRESS}}",
            "City, State, ZIP: {{ZIP_CODE}}",
            "Country: {{COUNTRY}}",
        ]
        text = intro + "\n".join(contact * 10)
        self.assertFalse(has_severe_repetition(text))


class TestRemoveRepeatedBlocks(TestCase):
    """Tests for remove_repeated_blocks()."""

    def test_none_input(self):
        self.assertIsNone(remove_repeated_blocks(None))

    def test_normal_text_unchanged(self):
        text = (
            "Dear Appeals Unit,\n"
            "I am writing to appeal the denial.\n"
            "The treatment was medically necessary.\n"
            "My doctor recommended this procedure.\n"
            "Evidence supports this course of action.\n"
            "Please reconsider your decision.\n"
            "Thank you for your time.\n"
            "Sincerely, Patient"
        )
        result = remove_repeated_blocks(text)
        self.assertEqual(result, text)

    def test_short_text_unchanged(self):
        """Fewer than 2*min_block_size non-empty lines should pass through."""
        text = "Line one\nLine two\nLine three\nLine four\nLine five"
        result = remove_repeated_blocks(text)
        self.assertEqual(result, text)

    def test_bullet_block_repeated_four_times(self):
        """Block of bullet points repeated 4x should keep only the first."""
        block = [
            "• Detailed history and examination",
            "• Medical decision-making of moderate complexity",
            "• Counseling and/or coordination of care",
            "• Services were medically necessary",
            "• Care was provided by an out-of-network provider",
        ]
        text = "\n".join(block * 4)
        result = remove_repeated_blocks(text)
        self.assertIsNotNone(result)
        # Should contain each bullet exactly once
        for line in block:
            self.assertEqual(result.count(line), 1)

    def test_contact_info_block_repeated(self):
        """Contact info block repeated 20x at end of good letter."""
        letter = (
            "Dear Appeals Unit,\n"
            "I am writing to formally appeal the denial of coverage.\n"
            "The services were medically necessary.\n"
            "Please reconsider your decision.\n"
            "Sincerely,\n"
            "Patient Name\n"
            "Member ID: XXXXX6627"
        )
        contact = [
            "Phone: {{PHONE_NUMBER}}",
            "Email: {{EMAIL_ADDRESS}}",
            "Fax: {{FAX_NUMBER}}",
            "Mail: {{ADDRESS}}",
            "City, State, ZIP: {{ZIP_CODE}}",
            "Country: {{COUNTRY}}",
        ]
        text = letter + "\n" + "\n".join(contact * 20)
        result = remove_repeated_blocks(text)
        self.assertIsNotNone(result)
        # Letter content preserved
        self.assertIn("Dear Appeals Unit,", result)
        self.assertIn("medically necessary", result)
        # Contact info appears only once
        self.assertEqual(result.count("Phone: {{PHONE_NUMBER}}"), 1)
        self.assertEqual(result.count("Email: {{EMAIL_ADDRESS}}"), 1)

    def test_mixed_content_preserved(self):
        """Unique content before and after a repeated block is preserved."""
        intro = "Introduction paragraph.\nThis is important context."
        block = [
            "• Point one about the case",
            "• Point two about the case",
            "• Point three about the case",
        ]
        outro = "In conclusion, please approve.\nThank you."
        text = intro + "\n" + "\n".join(block * 3) + "\n" + outro
        result = remove_repeated_blocks(text)
        self.assertIsNotNone(result)
        self.assertIn("Introduction paragraph.", result)
        self.assertIn("This is important context.", result)
        self.assertIn("In conclusion, please approve.", result)
        self.assertIn("Thank you.", result)
        for line in block:
            self.assertEqual(result.count(line), 1)

    def test_whitespace_normalization(self):
        """Blocks differing only in leading/trailing whitespace are detected."""
        block1 = "  • Point A\n  • Point B\n  • Point C"
        block2 = "• Point A\n• Point B\n• Point C"
        filler = "Some intro line.\nAnother line.\nThird line."
        text = filler + "\n" + block1 + "\n" + block2 + "\n" + block1
        result = remove_repeated_blocks(text)
        self.assertIsNotNone(result)
        # Should appear at most once after dedup
        self.assertLessEqual(result.count("Point A"), 1)

    def test_small_block_below_min_size_ignored(self):
        """A 2-line block repeating should not be removed (below min_block_size=3)."""
        block = "Line A\nLine B"
        text = "Intro line one.\nIntro line two.\nIntro line three.\n" + "\n".join(
            [block] * 5
        )
        result = remove_repeated_blocks(text)
        # 2-line block should NOT be removed
        self.assertGreaterEqual(result.count("Line A"), 3)

    def test_severe_repetition_keeps_first_copy(self):
        """Even heavily repeated blocks should salvage the first copy."""
        block = [
            "• Repeated point one",
            "• Repeated point two",
            "• Repeated point three",
        ]
        # 30 repetitions of 3 lines = 90 lines, only 3 unique
        text = "\n".join(block * 30)
        result = remove_repeated_blocks(text)
        self.assertIsNotNone(result)
        # Should contain each line exactly once
        for line in block:
            self.assertEqual(result.count(line), 1)


class TestRepetitionPenalty(TestCase):
    """Tests for repetition_penalty()."""

    def test_clean_text_no_penalty(self):
        """Text with no repetition should have zero penalty."""
        text = (
            "The patient was diagnosed with condition X. "
            "Treatment Y was recommended by Dr. Smith. "
            "The insurance company denied coverage. "
            "This denial is not supported by medical evidence. "
            "We request that you overturn this decision. "
            "The patient deserves appropriate care."
        )
        self.assertAlmostEqual(repetition_penalty(text), 0.0, places=2)

    def test_short_text_no_penalty(self):
        """Short text should not be penalized."""
        self.assertAlmostEqual(repetition_penalty("Hello world."), 0.0, places=2)

    def test_sentence_repetition_penalized(self):
        """Text with repeated sentences should have a positive penalty."""
        base = "This is a repeated claim."
        unique = [
            "First unique point here.",
            "Second unique point here.",
            "Third unique point here.",
            "Fourth unique point here.",
            "Fifth unique point here.",
        ]
        # 5 unique + base repeated 5 times = 10 sentences, 6 unique = 40% duplicates
        text = " ".join(unique + [base] * 5)
        penalty = repetition_penalty(text)
        self.assertGreater(penalty, 0.0)
        self.assertLess(penalty, 1.0)

    def test_block_repetition_penalized(self):
        """Text with a repeated block should have a positive penalty."""
        letter = "Dear Appeals Unit,\nI am writing to appeal.\nPlease review."
        block = [
            "• Point A about the case",
            "• Point B about the case",
            "• Point C about the case",
        ]
        text = letter + "\n" + "\n".join(block * 3)
        penalty = repetition_penalty(text)
        self.assertGreater(penalty, 0.0)

    def test_heavy_repetition_high_penalty(self):
        """Heavily repetitive text should have a penalty close to 1.0."""
        block = [
            "• Repeated point one",
            "• Repeated point two",
            "• Repeated point three",
        ]
        text = "\n".join(block * 20)
        penalty = repetition_penalty(text)
        self.assertGreater(penalty, 0.8)

    def test_penalty_increases_with_repetition(self):
        """More repetition should produce a higher penalty."""
        block = [
            "• Point A",
            "• Point B",
            "• Point C",
        ]
        filler = (
            "Dear Unit,\nI appeal.\nPlease review.\nThank you.\nSincerely.\nPatient."
        )
        text_2x = filler + "\n" + "\n".join(block * 2)
        text_5x = filler + "\n" + "\n".join(block * 5)
        self.assertGreater(repetition_penalty(text_5x), repetition_penalty(text_2x))

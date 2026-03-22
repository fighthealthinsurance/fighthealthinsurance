"""Tests for entity extraction identifier validation functions."""

import pytest

from fighthealthinsurance.generate_appeal import (
    identifier_found_in_text,
    is_plausible_identifier,
)


class TestIsPlausibleIdentifier:
    """Tests for is_plausible_identifier()."""

    def test_valid_alphanumeric_ids(self):
        """Valid plan/claim IDs should pass."""
        assert is_plausible_identifier("ABC123456") is True
        assert is_plausible_identifier("H5521-001") is True
        assert is_plausible_identifier("PLAN987654") is True
        assert is_plausible_identifier("CLM2024001234") is True
        assert is_plausible_identifier("2024-12345-A") is True
        assert is_plausible_identifier("GRP12345") is True

    def test_pure_digit_ids(self):
        """Pure digit IDs should pass."""
        assert is_plausible_identifier("12345") is True
        assert is_plausible_identifier("987654321") is True

    def test_pure_alpha_ids(self):
        """Pure alpha IDs that aren't English words should pass."""
        assert is_plausible_identifier("BCBSMA") is True
        assert is_plausible_identifier("HDHP") is True
        assert is_plausible_identifier("QWXYZ") is True

    def test_common_english_words_rejected(self):
        """Common English words that LLMs incorrectly return should be rejected."""
        assert is_plausible_identifier("covers") is False
        assert is_plausible_identifier("amount") is False
        assert is_plausible_identifier("denied") is False
        assert is_plausible_identifier("approved") is False
        assert is_plausible_identifier("patient") is False
        assert is_plausible_identifier("insurance") is False
        assert is_plausible_identifier("medical") is False
        assert is_plausible_identifier("the") is False
        assert is_plausible_identifier("plan") is False
        assert is_plausible_identifier("claim") is False
        assert is_plausible_identifier("health") is False
        assert is_plausible_identifier("services") is False
        assert is_plausible_identifier("payment") is False

    def test_multi_word_english_phrases_rejected(self):
        """Multi-word phrases where every word is English should be rejected."""
        assert is_plausible_identifier("health plan") is False
        assert is_plausible_identifier("not-covered") is False

    def test_none_and_empty(self):
        """None and empty values should be rejected."""
        assert is_plausible_identifier(None) is False
        assert is_plausible_identifier("") is False
        assert is_plausible_identifier("   ") is False

    def test_too_short(self):
        """Very short values should be rejected."""
        assert is_plausible_identifier("A1") is False
        assert is_plausible_identifier("1") is False

    def test_too_long(self):
        """Very long values (likely sentence fragments) should be rejected."""
        long_value = "A" * 40 + "1" + "B" * 20
        assert is_plausible_identifier(long_value) is False

    def test_labeled_strings_rejected(self):
        """Labeled strings like 'Plan ID: ABC123' should have labels stripped
        and be validated on the ID portion only, not accepted as-is."""
        # These should still return True because after stripping the label,
        # the remaining value 'ABC123' is a valid ID.
        assert is_plausible_identifier("Plan ID: ABC123") is True
        assert is_plausible_identifier("Claim #: XYZ789") is True
        assert is_plausible_identifier("Member: H5521-001") is True
        # But if the label prefix IS the entire value, reject it
        assert is_plausible_identifier("Plan ID:") is False
        assert is_plausible_identifier("Claim Number") is False

    def test_colons_in_value_rejected(self):
        """Colons indicate labels, not valid ID characters."""
        assert is_plausible_identifier("ABC:123") is False

    def test_special_characters_rejected(self):
        """Values with non-ID characters should be rejected."""
        assert is_plausible_identifier("plan@123") is False
        assert is_plausible_identifier("ID=12345") is False

    def test_whitespace_handling(self):
        """Leading/trailing whitespace should be stripped before validation."""
        assert is_plausible_identifier("  ABC123  ") is True
        assert is_plausible_identifier("  covers  ") is False

    def test_ids_with_allowed_separators(self):
        """IDs with hyphens, dots, slashes should pass."""
        assert is_plausible_identifier("ABC-123-456") is True
        assert is_plausible_identifier("ABC.123.456") is True
        assert is_plausible_identifier("ABC/123/456") is True
        assert is_plausible_identifier("ABC_123_456") is True


class TestIdentifierFoundInText:
    """Tests for identifier_found_in_text()."""

    def test_exact_match(self):
        """Exact match in text should return True."""
        assert identifier_found_in_text("ABC123", "Plan ID: ABC123") is True

    def test_hyphen_vs_space(self):
        """ID with hyphens should match text with spaces and vice versa."""
        assert identifier_found_in_text("H5521-001", "Plan ID: H5521 001") is True
        assert identifier_found_in_text("H5521 001", "Plan ID: H5521-001") is True

    def test_no_separator_match(self):
        """ID with separators should match text without separators."""
        assert identifier_found_in_text("H5521-001", "Plan ID: H5521001") is True

    def test_case_insensitive(self):
        """Match should be case-insensitive."""
        assert identifier_found_in_text("abc123", "Plan ID: ABC123") is True
        assert identifier_found_in_text("ABC123", "plan id: abc123") is True

    def test_not_found(self):
        """ID not in text should return False."""
        assert identifier_found_in_text("XYZ999", "Plan ID: ABC123") is False

    def test_empty_inputs(self):
        """Empty inputs should return False."""
        assert identifier_found_in_text("", "some text") is False
        assert identifier_found_in_text("ABC123", "") is False
        assert identifier_found_in_text("", "") is False

    def test_dot_separator_match(self):
        """ID with dots should match text with other separators."""
        assert identifier_found_in_text("ABC.123.456", "ABC-123-456") is True

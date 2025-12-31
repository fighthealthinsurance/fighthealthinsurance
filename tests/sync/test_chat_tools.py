"""Tests for chat tool handlers."""

import re
from unittest.mock import AsyncMock, MagicMock, patch
from django.test import TestCase

from fighthealthinsurance.chat.tools import (
    PUBMED_QUERY_REGEX,
    MEDICAID_INFO_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
    BaseTool,
    PubMedTool,
    MedicaidInfoTool,
    MedicaidEligibilityTool,
)


class TestToolPatterns(TestCase):
    """Test that tool regex patterns match expected formats."""

    def test_pubmed_query_pattern(self):
        """Test PubMed query pattern matches various formats."""
        test_cases = [
            ("pubmed_query: cancer treatment", "cancer treatment"),
            ("pubmed query: diabetes management", "diabetes management"),
            ("**pubmed_query: heart disease**", "heart disease"),
            ("[pubmed query: stroke prevention]", "stroke prevention"),
        ]
        for text, expected_query in test_cases:
            match = re.search(PUBMED_QUERY_REGEX, text, re.IGNORECASE)
            self.assertIsNotNone(match, f"Failed to match: {text}")
            self.assertEqual(match.group(1).strip(), expected_query)

    def test_medicaid_info_pattern(self):
        """Test Medicaid info pattern matches JSON format."""
        text = '**medicaid_info {"state": "California", "topic": "eligibility"}**'
        match = re.search(MEDICAID_INFO_REGEX, text, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn("California", match.group(1))

    def test_medicaid_eligibility_pattern(self):
        """Test Medicaid eligibility pattern matches JSON format."""
        text = 'medicaid_eligibility {"income": 30000, "household_size": 2}'
        match = re.search(MEDICAID_ELIGIBILITY_REGEX, text, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn("income", match.group(1))

    def test_appeal_pattern(self):
        """Test appeal creation pattern matches JSON format."""
        text = 'create_or_update_appeal {"insurance_company": "Aetna", "diagnosis": "diabetes"}'
        match = re.search(CREATE_OR_UPDATE_APPEAL_REGEX, text, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn("Aetna", match.group(1))

    def test_prior_auth_pattern(self):
        """Test prior auth pattern matches JSON format."""
        text = (
            'create_or_update_prior_auth {"treatment": "MRI", "diagnosis": "back pain"}'
        )
        match = re.search(
            CREATE_OR_UPDATE_PRIOR_AUTH_REGEX, text, re.DOTALL | re.MULTILINE
        )
        self.assertIsNotNone(match)
        self.assertIn("MRI", match.group(1))


class TestBaseTool(TestCase):
    """Test BaseTool abstract class functionality."""

    def test_detect_returns_none_without_pattern(self):
        """Test that detect returns None when pattern is empty."""
        mock_status = AsyncMock()

        class NoPatternTool(BaseTool):
            pattern = ""

            async def execute(self, match, response_text, context, **kwargs):
                return response_text, context

        tool = NoPatternTool(mock_status)
        result = tool.detect("any text here")
        self.assertIsNone(result)

    def test_clean_response_removes_match(self):
        """Test that clean_response removes the matched text."""
        mock_status = AsyncMock()

        class TestTool(BaseTool):
            pattern = r"REMOVE_THIS"

            async def execute(self, match, response_text, context, **kwargs):
                return response_text, context

        tool = TestTool(mock_status)
        match = re.search(r"REMOVE_THIS", "Keep this REMOVE_THIS and this")
        cleaned = tool.clean_response("Keep this REMOVE_THIS and this", match)
        self.assertEqual(cleaned, "Keep this  and this")


class TestPubMedTool(TestCase):
    """Test PubMedTool functionality."""

    def test_detect_pubmed_query(self):
        """Test PubMed tool detects query pattern."""
        mock_status = AsyncMock()
        tool = PubMedTool(mock_status)

        text = "Let me search for that. pubmed_query: cancer immunotherapy"
        match = tool.detect(text)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "cancer immunotherapy")

    def test_does_not_detect_invalid_query(self):
        """Test PubMed tool doesn't match on unrelated text."""
        mock_status = AsyncMock()
        tool = PubMedTool(mock_status)

        text = "This is just normal text without a query"
        match = tool.detect(text)

        self.assertIsNone(match)


class TestMedicaidInfoTool(TestCase):
    """Test MedicaidInfoTool functionality."""

    def test_detect_medicaid_info(self):
        """Test Medicaid info tool detects pattern."""
        mock_status = AsyncMock()
        tool = MedicaidInfoTool(mock_status)

        text = '**medicaid_info {"state": "Texas"}**'
        match = tool.detect(text)

        self.assertIsNotNone(match)
        self.assertIn("Texas", match.group(1))

    def test_detect_all_finds_multiple(self):
        """Test detect_all finds multiple matches."""
        mock_status = AsyncMock()
        tool = MedicaidInfoTool(mock_status)

        text = '**medicaid_info {"state": "CA"}** and **medicaid_info {"state": "TX"}**'
        matches = tool.detect_all(text)

        self.assertEqual(len(matches), 2)


class TestMedicaidEligibilityTool(TestCase):
    """Test MedicaidEligibilityTool functionality."""

    def test_detect_medicaid_eligibility(self):
        """Test Medicaid eligibility tool detects pattern."""
        mock_status = AsyncMock()
        tool = MedicaidEligibilityTool(mock_status)

        text = 'medicaid_eligibility {"income": 25000}'
        match = tool.detect(text)

        self.assertIsNotNone(match)
        self.assertIn("income", match.group(1))

    def test_build_eligibility_info_eligible(self):
        """Test eligibility info text generation for eligible user."""
        mock_status = AsyncMock()
        tool = MedicaidEligibilityTool(mock_status)

        info = tool._build_eligibility_info(
            eligible_2025=True,
            eligible_2026=True,
            medicare=False,
            alternatives=[],
            missing=[],
        )

        self.assertIn("eligible for medicaid", info.lower())
        self.assertIn("2025", info)
        self.assertIn("2026", info)

    def test_build_eligibility_info_with_missing(self):
        """Test eligibility info text when questions are missing."""
        mock_status = AsyncMock()
        tool = MedicaidEligibilityTool(mock_status)

        info = tool._build_eligibility_info(
            eligible_2025=False,
            eligible_2026=False,
            medicare=False,
            alternatives=[],
            missing=["income", "household_size"],
        )

        self.assertIn("questions to ask", info)

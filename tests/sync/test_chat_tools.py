"""Tests for chat tool handlers."""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch
from django.test import TestCase

from fighthealthinsurance.chat.tools import (
    PUBMED_QUERY_REGEX,
    MEDICAID_INFO_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
    FETCH_DOC_REGEX,
    BaseTool,
    PubMedTool,
    MedicaidInfoTool,
    MedicaidEligibilityTool,
    DocFetcherTool,
)
from fighthealthinsurance.chat.tools.doc_fetcher_tool import (
    MAX_FETCHES_PER_SESSION,
    _sanitize_url_for_display,
    validate_url,
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


class TestDocFetcherPatterns(TestCase):
    """Test FETCH_DOC_REGEX pattern matching."""

    def test_fetch_doc_with_stars(self):
        """Test fetch_doc pattern matches with ** markers."""
        text = '**fetch_doc {"url": "https://example.com/plan.pdf"}**'
        match = re.search(FETCH_DOC_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("https://example.com/plan.pdf", match.group(1))

    def test_fetch_doc_without_stars(self):
        """Test fetch_doc pattern matches without markers."""
        text = 'fetch_doc {"url": "https://example.com/doc.html"}'
        match = re.search(FETCH_DOC_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("https://example.com/doc.html", match.group(1))

    def test_fetch_doc_in_sentence(self):
        """Test fetch_doc pattern matches when embedded in a sentence."""
        text = 'Let me look that up. **fetch_doc {"url": "https://example.com/g.pdf"}** I found it.'
        match = re.search(FETCH_DOC_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)

    def test_no_false_positive(self):
        """Test fetch_doc pattern does not match unrelated text."""
        text = "Please fetch the document from the website."
        match = re.search(FETCH_DOC_REGEX, text, re.IGNORECASE)
        self.assertIsNone(match)


class TestDocFetcherTool(TestCase):
    """Test DocFetcherTool detection."""

    def test_detect_fetch_doc(self):
        """Test DocFetcherTool detects the fetch_doc pattern."""
        mock_status = AsyncMock()
        tool = DocFetcherTool(mock_status)

        text = '**fetch_doc {"url": "https://example.com/plan.pdf"}**'
        match = tool.detect(text)

        self.assertIsNotNone(match)

    def test_does_not_detect_unrelated(self):
        """Test DocFetcherTool does not match on unrelated text."""
        mock_status = AsyncMock()
        tool = DocFetcherTool(mock_status)

        text = "This is just normal text without a tool call"
        match = tool.detect(text)

        self.assertIsNone(match)


class TestValidateUrl(TestCase):
    """Test SSRF protection in validate_url."""

    def _run(self, coro):
        """Helper to run async code in sync tests."""
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_rejects_non_http_scheme(self):
        """Test that non-HTTP(S) schemes are rejected."""
        with self.assertRaises(ValueError):
            self._run(validate_url("ftp://example.com/file.pdf"))
        with self.assertRaises(ValueError):
            self._run(validate_url("file:///etc/passwd"))

    def test_rejects_localhost(self):
        """Test that localhost is rejected."""
        with self.assertRaises(ValueError):
            self._run(validate_url("http://localhost/secret"))
        with self.assertRaises(ValueError):
            self._run(validate_url("http://127.0.0.1/secret"))

    def test_rejects_local_suffix(self):
        """Test that .local domains are rejected."""
        with self.assertRaises(ValueError):
            self._run(validate_url("http://myhost.local/doc.pdf"))

    def test_rejects_empty_hostname(self):
        """Test that URLs without hostname are rejected."""
        with self.assertRaises(ValueError):
            self._run(validate_url("http:///path"))

    def test_accepts_valid_https_url(self):
        """Test that valid HTTPS URLs pass validation."""
        # Mock DNS to avoid network dependency and return a public IP
        with patch(
            "fighthealthinsurance.chat.tools.doc_fetcher_tool.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        ):
            # Should not raise
            self._run(validate_url("https://www.example.com/document.pdf"))

    def test_accepts_valid_http_url(self):
        """Test that valid HTTP URLs pass validation."""
        # Mock DNS to avoid network dependency and return a public IP
        with patch(
            "fighthealthinsurance.chat.tools.doc_fetcher_tool.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        ):
            # Should not raise
            self._run(validate_url("http://www.example.com/document.pdf"))

    def test_rejects_unresolvable_hostname(self):
        """Test that unresolvable hostnames are rejected."""
        import socket as socket_mod

        with patch(
            "fighthealthinsurance.chat.tools.doc_fetcher_tool.socket.getaddrinfo",
            side_effect=socket_mod.gaierror("Name or service not known"),
        ):
            with self.assertRaises(ValueError):
                self._run(validate_url("https://some-unresolvable-host.example/doc"))

    def test_rejects_private_ip_after_resolution(self):
        """Test that a hostname resolving to a private IP is rejected."""
        with patch(
            "fighthealthinsurance.chat.tools.doc_fetcher_tool.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("10.0.0.1", 0))],
        ):
            with self.assertRaises(ValueError):
                self._run(validate_url("https://sneaky.example.com/doc"))


class TestSanitizeUrlForDisplay(TestCase):
    """Test URL sanitization for status messages."""

    def test_strips_query_params(self):
        """Test that query parameters are stripped."""
        url = "https://example.com/doc.pdf?token=secret123&user=alice"
        sanitized = _sanitize_url_for_display(url)
        self.assertEqual(sanitized, "https://example.com/doc.pdf")
        self.assertNotIn("secret123", sanitized)
        self.assertNotIn("alice", sanitized)

    def test_strips_fragment(self):
        """Test that URL fragments are stripped."""
        url = "https://example.com/doc.pdf#page=5"
        sanitized = _sanitize_url_for_display(url)
        self.assertEqual(sanitized, "https://example.com/doc.pdf")

    def test_strips_both(self):
        """Test that both query and fragment are stripped."""
        url = "https://example.com/doc.pdf?key=val#section"
        sanitized = _sanitize_url_for_display(url)
        self.assertEqual(sanitized, "https://example.com/doc.pdf")

    def test_preserves_path(self):
        """Test that path components are preserved."""
        url = "https://example.com/path/to/resource.pdf"
        sanitized = _sanitize_url_for_display(url)
        self.assertEqual(sanitized, "https://example.com/path/to/resource.pdf")


class TestDocFetcherRateLimit(TestCase):
    """Test rate limiting in DocFetcherTool."""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_respects_rate_limit(self):
        """Test that rate limit blocks fetches after MAX_FETCHES_PER_SESSION."""
        mock_status = AsyncMock()
        # Pre-fill the counter to the max
        fetch_count = [MAX_FETCHES_PER_SESSION]
        tool = DocFetcherTool(mock_status, fetch_count=fetch_count)

        # Mock the fetcher so we can detect if it's called
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock()

        match = re.search(
            FETCH_DOC_REGEX,
            '**fetch_doc {"url": "https://example.com/doc.pdf"}**',
            re.IGNORECASE,
        )
        response, context = self._run(tool.execute(match, "response text", "context"))

        # Fetcher should NOT have been called
        tool.fetcher.fetch_and_extract_text.assert_not_called()
        # Status message should mention the limit
        mock_status.assert_awaited()
        status_calls = [c.args[0] for c in mock_status.await_args_list]
        self.assertTrue(
            any("limit" in msg.lower() for msg in status_calls),
            f"Expected rate limit message in {status_calls}",
        )

    def test_counter_increments_before_fetch(self):
        """Test that counter increments before fetch (so failures count)."""
        mock_status = AsyncMock()
        fetch_count = [0]
        tool = DocFetcherTool(mock_status, fetch_count=fetch_count)

        # Make the fetcher raise an exception
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock(
            side_effect=Exception("Network error")
        )

        match = re.search(
            FETCH_DOC_REGEX,
            '**fetch_doc {"url": "https://www.example.com/doc.pdf"}**',
            re.IGNORECASE,
        )

        # Patch validate_url to skip DNS resolution
        with patch(
            "fighthealthinsurance.chat.tools.doc_fetcher_tool.validate_url",
            new=AsyncMock(return_value=None),
        ):
            response, context = self._run(
                tool.execute(match, "response text", "context")
            )

        # Counter should have incremented even though fetch failed
        self.assertEqual(fetch_count[0], 1)


class TestDocFetcherExecute(TestCase):
    """Test DocFetcherTool.execute end-to-end with mocked fetcher."""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_invalid_json_returns_cleanly(self):
        """Test that invalid JSON is handled gracefully."""
        mock_status = AsyncMock()
        tool = DocFetcherTool(mock_status)
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock()

        match = re.search(
            FETCH_DOC_REGEX,
            "**fetch_doc {not valid json}**",
            re.IGNORECASE,
        )
        self.assertIsNotNone(match)
        response, context = self._run(tool.execute(match, "original", "ctx"))
        # Fetcher should not have been called
        tool.fetcher.fetch_and_extract_text.assert_not_called()
        # Response should have the tool call stripped
        self.assertNotIn("fetch_doc", response)

    def test_empty_url_returns_cleanly(self):
        """Test that empty URL in JSON is handled gracefully."""
        mock_status = AsyncMock()
        tool = DocFetcherTool(mock_status)
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock()

        match = re.search(
            FETCH_DOC_REGEX,
            '**fetch_doc {"url": ""}**',
            re.IGNORECASE,
        )
        response, context = self._run(tool.execute(match, "original", "ctx"))
        tool.fetcher.fetch_and_extract_text.assert_not_called()

    def test_successful_fetch_appends_to_context(self):
        """Test that a successful fetch appends extracted text to context."""
        mock_status = AsyncMock()
        tool = DocFetcherTool(mock_status)
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock(
            return_value=("Extracted document text", "pdf")
        )

        match = re.search(
            FETCH_DOC_REGEX,
            '**fetch_doc {"url": "https://www.example.com/doc.pdf"}**',
            re.IGNORECASE,
        )

        with patch(
            "fighthealthinsurance.chat.tools.doc_fetcher_tool.validate_url",
            new=AsyncMock(return_value=None),
        ):
            response, context = self._run(
                tool.execute(
                    match,
                    '**fetch_doc {"url": "https://www.example.com/doc.pdf"}**',
                    "",
                )
            )

        self.assertIn("Extracted document text", context)
        self.assertIn("https://www.example.com/doc.pdf", context)
        # Tool call should be stripped from response
        self.assertNotIn("fetch_doc", response)

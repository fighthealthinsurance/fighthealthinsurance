"""
Tests for the FinancialAssistanceTool chat handler.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

from django.test import TestCase

from fighthealthinsurance.chat.tools import (
    FINANCIAL_ASSISTANCE_REGEX,
    FinancialAssistanceTool,
)


def _run(coro):
    """Run an async coroutine in a sync test."""
    return asyncio.run(coro)


def _make_tool(call_llm_callback=None):
    return FinancialAssistanceTool(
        send_status_message=AsyncMock(),
        call_llm_callback=call_llm_callback,
    )


class TestFinancialAssistancePattern(TestCase):
    """Regex pattern detection."""

    def test_pattern_matches_basic_invocation(self):
        text = 'Some text **financial_assistance {"drug": "Wegovy"}** more text'
        match = re.search(FINANCIAL_ASSISTANCE_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), '{"drug": "Wegovy"}')

    def test_pattern_matches_without_asterisks(self):
        text = 'financial_assistance {"diagnosis": "cancer"}'
        match = re.search(FINANCIAL_ASSISTANCE_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)

    def test_pattern_does_not_match_other_tools(self):
        text = 'pubmed_query: x medicaid_info {"state": "CA"}'
        match = re.search(FINANCIAL_ASSISTANCE_REGEX, text, re.IGNORECASE)
        self.assertIsNone(match)


class TestFinancialAssistanceToolHandling(TestCase):
    """End-to-end .handle() behavior."""

    def test_returns_unchanged_when_no_match(self):
        tool = _make_tool()
        text = "Just a normal LLM response with no tool call."
        out_text, out_ctx, handled = _run(tool.handle(text, "ctx"))
        self.assertFalse(handled)
        self.assertEqual(out_text, text)
        self.assertEqual(out_ctx, "ctx")

    def test_handle_runs_search_and_returns_when_match(self):
        # No call_llm_callback - tool should still run, strip the call, and
        # send completion status.
        tool = _make_tool()
        text = '**financial_assistance {"drug": "Wegovy"}**'
        out_text, _ctx, handled = _run(tool.handle(text, ""))
        self.assertTrue(handled)
        self.assertNotIn("financial_assistance", out_text)
        tool.send_status_message.assert_any_call(
            "Looking up financial assistance options..."
        )
        tool.send_status_message.assert_any_call(
            "Financial assistance lookup completed."
        )

    def test_invalid_json_sends_friendly_error(self):
        tool = _make_tool()
        text = "**financial_assistance {not valid json}**"
        out_text, _ctx, handled = _run(tool.handle(text, ""))
        self.assertTrue(handled)
        tool.send_status_message.assert_any_call(
            "Error processing financial assistance lookup: invalid JSON."
        )
        self.assertIn("couldn't parse", out_text.lower())

    def test_calls_llm_callback_with_directory_context(self):
        callback = AsyncMock(return_value=("LLM follow-up reply", "extra context"))
        tool = _make_tool(call_llm_callback=callback)

        text = (
            "Sure, I can help with cost.\n"
            '**financial_assistance {"drug": "Wegovy", "diagnosis": "obesity"}**'
        )
        history: list = []
        out_text, _ctx, handled = _run(
            tool.handle(
                text,
                "",
                model_backends=MagicMock(),
                current_message_for_llm="Can I afford Wegovy?",
                history_for_llm=history,
            )
        )
        self.assertTrue(handled)
        callback.assert_awaited_once()
        callback_args, _ = callback.call_args
        info_text = callback_args[1]
        # The LLM gets a context with the canonical drug name and at least
        # one matched program.
        self.assertIn("wegovy", info_text.lower())
        self.assertIn("Wegovy Savings Card", info_text)
        # General programs are always included
        self.assertIn("NeedyMeds", info_text)
        # OOP-max reminder appears in the action text appended to info_text
        self.assertIn("out-of-pocket", info_text.lower())
        # The user's message and the prior response are appended to history
        # before the recursive LLM call
        self.assertTrue(any(m["role"] == "user" for m in history))
        # Original cleaned response (with tool call removed) is concatenated
        # with the follow-up LLM response
        self.assertIn("LLM follow-up reply", out_text)
        self.assertIn("Sure, I can help with cost", out_text)
        self.assertNotIn("financial_assistance", out_text)

    def test_search_failure_does_not_crash_handler(self):
        from fighthealthinsurance import financial_assistance_directory

        original = financial_assistance_directory.search

        def boom(**kwargs):
            raise RuntimeError("simulated failure")

        financial_assistance_directory.search = boom  # type: ignore[assignment]
        try:
            tool = _make_tool()
            text = '**financial_assistance {"drug": "Wegovy"}**'
            out_text, _ctx, handled = _run(tool.handle(text, ""))
            self.assertTrue(handled)
            self.assertNotIn("financial_assistance", out_text)
            tool.send_status_message.assert_any_call(
                "Financial assistance lookup failed; continuing without it."
            )
        finally:
            financial_assistance_directory.search = original  # type: ignore[assignment]

    def test_state_param_passed_through_as_state_abbreviation(self):
        from fighthealthinsurance import financial_assistance_directory

        captured = {}
        original = financial_assistance_directory.search

        def fake_search(**kwargs):
            captured.update(kwargs)
            return original()

        financial_assistance_directory.search = fake_search  # type: ignore[assignment]
        try:
            tool = _make_tool()
            _run(tool.handle('**financial_assistance {"state": "CA"}**', ""))
            self.assertEqual(captured.get("state_abbreviation"), "CA")
        finally:
            financial_assistance_directory.search = original  # type: ignore[assignment]


class TestFormatResultsForLLM(TestCase):
    """Direct tests for the _format_results_for_llm helper."""

    def test_includes_all_section_headers_when_populated(self):
        from fighthealthinsurance.financial_assistance_directory import search

        results = search(drug="Wegovy", diagnosis="cancer")
        formatted = FinancialAssistanceTool._format_results_for_llm(results)
        self.assertIn("copay foundations", formatted.lower())
        self.assertIn("manufacturer", formatted.lower())
        self.assertIn("safety-net", formatted.lower())
        self.assertIn("wegovy", formatted.lower())
        self.assertIn("Wegovy Savings Card", formatted)
        self.assertIn("CancerCare", formatted)

    def test_includes_state_medicaid_block(self):
        from fighthealthinsurance.financial_assistance_directory import (
            FinancialAssistanceResults,
        )

        results = FinancialAssistanceResults(
            state_medicaid_name="Medi-Cal",
            state_medicaid_url="https://www.medi-cal.ca.gov/",
            state_medicaid_phone="1-800-541-5555",
        )
        formatted = FinancialAssistanceTool._format_results_for_llm(results)
        self.assertIn("Medi-Cal", formatted)
        self.assertIn("https://www.medi-cal.ca.gov/", formatted)
        self.assertIn("1-800-541-5555", formatted)

    def test_handles_empty_results(self):
        from fighthealthinsurance.financial_assistance_directory import (
            FinancialAssistanceResults,
        )

        results = FinancialAssistanceResults()
        # Should not crash; produces a minimal lead-in line.
        formatted = FinancialAssistanceTool._format_results_for_llm(results)
        self.assertIn("curated directory", formatted)

"""
Tests for the FinancialAssistanceTool chat handler.
"""

import re
from unittest.mock import AsyncMock, MagicMock

from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.chat.tools import (
    FINANCIAL_ASSISTANCE_REGEX,
    FinancialAssistanceTool,
)


async def _await(awaitable):
    return await awaitable


def _run(awaitable):
    """Run an async awaitable in a sync test.

    Project guidelines require asgiref.async_to_sync as the async/sync
    bridge rather than asyncio.run (see tests/sync/test_prior_auth_api.py
    and tests/sync/test_insurance_company_plan.py for the same pattern).
    """
    return async_to_sync(_await)(awaitable)


def _make_tool(call_llm_callback=None):
    return FinancialAssistanceTool(
        send_status_message=AsyncMock(),
        call_llm_callback=call_llm_callback,
    )


class TestFinancialAssistancePattern(TestCase):
    """Regex pattern detection.

    The pattern is now a lookahead at the opening `{`; the JSON body is
    parsed at runtime via ``FinancialAssistanceTool._parse_payload`` so
    payloads with nested objects / `}` inside strings stay intact.
    """

    def test_pattern_matches_basic_invocation(self):
        text = 'Some text **financial_assistance {"drug": "Wegovy"}** more text'
        match = re.search(FINANCIAL_ASSISTANCE_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)
        assert match is not None  # narrow for type-checkers below
        # Lookahead means match.end() lands on the opening `{` and the
        # regex captures nothing past that point.
        self.assertEqual(text[match.end()], "{")

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

    def test_handle_parses_payload_with_closing_brace_in_string(self):
        """Regression: payloads whose string values contain ``}`` must parse.

        The previous ``\\{[^}]*\\}`` regex truncated at the first ``}`` and
        broke valid tool calls — fixed by switching the pattern to a
        lookahead and using ``json.JSONDecoder().raw_decode`` to bound the
        actual JSON object.
        """
        callback = AsyncMock(return_value=("ok", None))
        tool = _make_tool(call_llm_callback=callback)
        text = (
            "Sure, looking up help.\n"
            "**financial_assistance "
            '{"drug": "Wegovy", '
            '"denial_text": "Step 1) Plan says: } not on formulary"}**'
            " trailing prose after"
        )
        out_text, _ctx, handled = _run(
            tool.handle(
                text,
                "",
                model_backends=MagicMock(),
                current_message_for_llm="how do I afford this?",
                history_for_llm=[],
            )
        )
        self.assertTrue(handled)
        # The LLM callback fires only when JSON parse succeeded.
        callback.assert_awaited_once()
        # The cleaned response strips the whole tool call (including the
        # closing ``}**``) but preserves prose before and after.
        self.assertNotIn("financial_assistance", out_text)
        self.assertNotIn("Step 1)", out_text)
        self.assertIn("Sure, looking up help.", out_text)
        self.assertIn("trailing prose after", out_text)

    def test_handle_parses_payload_with_nested_object(self):
        """Nested JSON objects (e.g. structured `state` info) must not be
        truncated by the regex — same root cause as the closing-brace-in-
        string regression."""
        callback = AsyncMock(return_value=("ok", None))
        tool = _make_tool(call_llm_callback=callback)
        text = (
            "**financial_assistance "
            '{"drug": "Wegovy", "meta": {"locale": "en-US", "src": "chat"}}**'
        )
        _out_text, _ctx, handled = _run(
            tool.handle(
                text,
                "",
                model_backends=MagicMock(),
                current_message_for_llm="cost help?",
                history_for_llm=[],
            )
        )
        self.assertTrue(handled)
        callback.assert_awaited_once()

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

    def test_includes_pharmacy_discount_block_when_suggestion_present(self):
        # The chat tool combines directory results with the pharmacy_coupon_
        # detector's suggestion so the LLM can also recommend GoodRx, Cost
        # Plus, and Amazon Pharmacy as a cash-pay bridge.
        from fighthealthinsurance.financial_assistance_directory import search
        from fighthealthinsurance.pharmacy_coupon_detector import (
            suggest_for_denial,
        )

        results = search(drug="Wegovy")
        suggestion = suggest_for_denial(drug="Wegovy")
        formatted = FinancialAssistanceTool._format_results_for_llm(results, suggestion)
        # GoodRx / Cost Plus / Amazon must all be in the LLM context
        # ("Amazon Search" because the drug-detected affiliate URL points
        # at amazon.com/s rather than pharmacy.amazon.com).
        self.assertIn("GoodRx", formatted)
        self.assertIn("Mark Cuban Cost Plus Drugs", formatted)
        self.assertIn("Amazon Search", formatted)
        # OOP-max caveat surfaces too
        self.assertIn("out-of-pocket maximum", formatted.lower())

    def test_pharmacy_block_omitted_when_suggestion_is_none(self):
        from fighthealthinsurance.financial_assistance_directory import search

        results = search(diagnosis="cancer")
        formatted = FinancialAssistanceTool._format_results_for_llm(results, None)
        # No pharmacy discount header / GoodRx link when no suggestion
        self.assertNotIn("Pharmacy discount", formatted)
        self.assertNotIn("GoodRx", formatted)


class TestPharmacyDiscountInclusion(TestCase):
    """End-to-end: invoking the tool with a drug surfaces pharmacy options."""

    def test_handle_includes_pharmacy_options_in_llm_context(self):
        callback = AsyncMock(return_value=("ok", None))
        tool = _make_tool(call_llm_callback=callback)
        text = '**financial_assistance {"drug": "Wegovy"}**'
        _run(
            tool.handle(
                text,
                "",
                model_backends=MagicMock(),
                current_message_for_llm="how can I afford Wegovy?",
                history_for_llm=[],
            )
        )
        callback.assert_awaited_once()
        info_text = callback.call_args[0][1]
        # GoodRx + Cost Plus + Amazon must show up alongside the directory
        # programs, since the prompt promises pharmacy discount options.
        self.assertIn("GoodRx", info_text)
        self.assertIn("Mark Cuban Cost Plus Drugs", info_text)
        self.assertIn("Amazon Search", info_text)

    def test_handle_does_not_treat_non_drug_as_drug(self):
        """If the LLM populates `drug` with arbitrary text that isn't a
        recognized medication (e.g. "MRI of knee"), the chat tool must
        NOT build pharmacy URLs around that token. We only forward
        `drug=` to suggest_for_denial when detect_drug() recognizes it,
        otherwise let detection fall back to denial_text/diagnosis."""
        callback = AsyncMock(return_value=("ok", None))
        tool = _make_tool(call_llm_callback=callback)
        text = '**financial_assistance {"drug": "MRI of knee"}**'
        _run(
            tool.handle(
                text,
                "",
                model_backends=MagicMock(),
                current_message_for_llm="how do I get my MRI covered?",
                history_for_llm=[],
            )
        )
        callback.assert_awaited_once()
        info_text = callback.call_args[0][1]
        # The pharmacy block, if present at all, must not embed "MRI" or
        # "knee" into a fake drug-specific GoodRx/Amazon link. Easiest
        # invariant: the bogus drug name never appears as a URL slug.
        self.assertNotIn("goodrx.com/mri", info_text.lower())
        self.assertNotIn("amazon.com/s?k=mri", info_text.lower())

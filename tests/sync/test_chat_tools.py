"""Tests for chat tool handlers."""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch
from django.test import TestCase

from fighthealthinsurance.chat.tools import (
    CLINICAL_TRIALS_QUERY_REGEX,
    PUBMED_QUERY_REGEX,
    MEDICAID_INFO_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
    FETCH_DOC_REGEX,
    RXNORM_LOOKUP_REGEX,
    USPSTF_LOOKUP_REGEX,
    BaseTool,
    count_tool_invocations,
    ClinicalTrialsTool,
    PubMedTool,
    MedicaidInfoTool,
    MedicaidEligibilityTool,
    DocFetcherTool,
    RxNormLookupTool,
    USPSTFLookupTool,
)
from fighthealthinsurance.medicaid_api import (
    BASE_ELIGIBILITY_YEAR,
    DEFAULT_TARGET_YEAR,
    YearVerdict,
    current_eligibility_year,
    is_eligible,
    summarize_eligibility_inputs,
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

    def test_pubmed_query_pattern_ignores_prose(self):
        """An offer to search is not a search.

        The colon used to be optional, so "I can run a pubmed query for you"
        fired the handler AND earned the fan-out's tool-call bonus -- letting
        a candidate that offered to use a tool outscore one that used it.
        """
        prose = [
            "I can run a pubmed query for you if that would help.",
            "Want me to do a pubmed query?",
            "A PubMed query might turn up supporting studies.",
        ]
        for text in prose:
            with self.subTest(text=text):
                self.assertIsNone(re.search(PUBMED_QUERY_REGEX, text, re.IGNORECASE))

    def test_count_tool_invocations_counts_distinct_tools(self):
        text = (
            "Let me look at a couple of things.\n"
            '**medicaid_eligibility {"state": "CA"}**\n'
            "[*pubmed query: metformin*]"
        )
        self.assertEqual(count_tool_invocations(text), 2)

    def test_count_tool_invocations_finds_an_anchored_call_after_prose(self):
        # CREATE_OR_UPDATE_* are `^...$`-anchored, so a flag-less re.search
        # only ever saw them at the very start of a reply.
        text = 'Sure, here it is.\n**create_or_update_appeal**{"a": 1}\n'
        self.assertEqual(count_tool_invocations(text), 1)

    def test_count_tool_invocations_ignores_a_bare_mention(self):
        self.assertEqual(
            count_tool_invocations("I will use create_or_update_appeal shortly"), 0
        )

    def test_count_tool_invocations_handles_empty_input(self):
        self.assertEqual(count_tool_invocations(""), 0)
        self.assertEqual(count_tool_invocations(None), 0)

    def test_clinical_trials_query_pattern(self):
        """Test ClinicalTrials.gov query pattern matches various formats."""
        test_cases = [
            (
                "clinical_trials_query: pembrolizumab melanoma",
                "pembrolizumab melanoma",
            ),
            (
                "clinical trials query: car-t lymphoma",
                "car-t lymphoma",
            ),
            (
                "**clinical_trials_query: gene therapy SMA**",
                "gene therapy SMA",
            ),
            (
                "[clinical trials query: tirzepatide obesity]",
                "tirzepatide obesity",
            ),
            (
                "*clinical_trial_query: TMS depression*",
                "TMS depression",
            ),
        ]
        for text, expected_query in test_cases:
            match = re.search(CLINICAL_TRIALS_QUERY_REGEX, text, re.IGNORECASE)
            self.assertIsNotNone(match, f"Failed to match: {text}")
            self.assertEqual(match.group(1).strip(), expected_query)

    def test_clinical_trials_pattern_does_not_match_pubmed(self):
        """ClinicalTrials regex must not be triggered by a plain pubmed_query."""
        text = "pubmed_query: heart failure"
        match = re.search(CLINICAL_TRIALS_QUERY_REGEX, text, re.IGNORECASE)
        self.assertIsNone(match)

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

    def test_rxnorm_lookup_pattern(self):
        """Test rxnorm_lookup pattern matches drug-name forms."""
        test_cases = [
            ("rxnorm_lookup: Lipitor", "Lipitor"),
            ("rxnorm lookup: metformin 500mg", "metformin 500mg"),
            ("**rxnorm_lookup: glucophage**", "glucophage"),
            ("[rxnorm lookup: omeprazole]", "omeprazole"),
        ]
        for text, expected in test_cases:
            match = re.search(RXNORM_LOOKUP_REGEX, text, re.IGNORECASE)
            self.assertIsNotNone(match, f"Failed to match: {text}")
            self.assertEqual(match.group(1).strip(), expected)

    def test_rxnorm_lookup_pattern_rejects_prose(self):
        """Pattern should not match natural-language mentions without an
        explicit ``rxnorm_lookup:`` directive."""
        # No colon — should not match.
        self.assertIsNone(
            re.search(
                RXNORM_LOOKUP_REGEX,
                "RxNorm lookup for Lipitor",
                re.IGNORECASE,
            )
        )
        # Random sentence containing the words but not in tool form.
        self.assertIsNone(
            re.search(
                RXNORM_LOOKUP_REGEX,
                "I will check rxnorm lookup later",
                re.IGNORECASE,
            )
        )


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

    def test_recent_search_uses_dynamic_year(self):
        """Recent search should use a rolling lower-bound year string."""
        mock_status = AsyncMock()
        mock_pubmed_tools = MagicMock()
        mock_pubmed_tools.find_pubmed_article_ids_for_query = AsyncMock(
            side_effect=[["1", "2"], ["2", "3"]]
        )
        tool = PubMedTool(mock_status, pubmed_tools=mock_pubmed_tools)

        with patch.object(tool, "_recent_since_year", return_value="2024"):
            article_ids = asyncio.run(tool._search_articles("test query"))

        self.assertTrue(set(article_ids).issuperset({"1", "2", "3"}))
        recent_call = (
            mock_pubmed_tools.find_pubmed_article_ids_for_query.call_args_list[0]
        )
        self.assertEqual(recent_call.kwargs["since"], "2024")

    def test_recent_since_year_uses_configurable_window(self):
        mock_status = AsyncMock()
        tool = PubMedTool(mock_status)

        with patch("fighthealthinsurance.chat.tools.pubmed_tool.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.year = 2030
            mock_dt.now.return_value = mock_now

            with patch.dict(
                "os.environ",
                {"PUBMED_RECENT_WINDOW_YEARS": "3"},
                clear=False,
            ):
                self.assertEqual(tool._recent_since_year(), "2027")


class TestClinicalTrialsTool(TestCase):
    """Test ClinicalTrialsTool detection and helper rendering."""

    def test_detect_clinical_trials_query(self):
        mock_status = AsyncMock()
        tool = ClinicalTrialsTool(mock_status)

        text = (
            "Insurer says it's experimental. "
            "**clinical_trials_query: pembrolizumab melanoma**"
        )
        match = tool.detect(text)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "pembrolizumab melanoma")

    def test_does_not_detect_unrelated(self):
        mock_status = AsyncMock()
        tool = ClinicalTrialsTool(mock_status)

        text = "I think we should try a clinical study but no tool token here."
        match = tool.detect(text)

        self.assertIsNone(match)

    def test_clean_response_strips_trailing_delimiters(self):
        """The regex must consume closing ** / ] so clean_response leaves no
        stray markdown in the user-visible reply."""
        mock_status = AsyncMock()
        tool = ClinicalTrialsTool(mock_status)
        cases = [
            "**clinical_trials_query: gene therapy SMA**",
            "[clinical_trials_query: tirzepatide obesity]",
            "*clinical_trial_query: TMS depression*",
        ]
        for text in cases:
            match = tool.detect(text)
            self.assertIsNotNone(match, f"Failed to match: {text}")
            self.assertEqual(tool.clean_response(text, match), "")

    def test_build_trials_context_includes_guidance(self):
        """The context block should remind the LLM that a trial != coverage."""
        mock_status = AsyncMock()
        tool = ClinicalTrialsTool(mock_status)

        trial = MagicMock()
        trial.nct_id = "NCT01234567"
        trial.brief_title = "A Study of Test Drug in Test Condition"
        trial.overall_status = "RECRUITING"
        trial.phases = "PHASE3"
        trial.study_type = "INTERVENTIONAL"
        trial.conditions = "Test Condition"
        trial.interventions = "Drug: Test Drug"
        trial.has_results = False
        trial.start_date = "2023-01-01"
        trial.completion_date = "2026-01-01"
        trial.brief_summary = "This trial evaluates Test Drug for Test Condition."
        trial.study_url = "https://clinicaltrials.gov/study/NCT01234567"

        rendered = tool._build_trials_context([trial])

        self.assertIn("clinicaltrialscontext", rendered)
        self.assertIn("NCT01234567", rendered)
        self.assertIn("does not by itself", rendered)
        self.assertIn("https://clinicaltrials.gov/study/NCT01234567", rendered)

    def test_build_trials_context_empty_when_no_trials(self):
        mock_status = AsyncMock()
        tool = ClinicalTrialsTool(mock_status)
        self.assertEqual(tool._build_trials_context([]), "")

    def test_regex_stops_at_newline_not_swallowing_trailing_prose(self):
        """An LLM that puts the token on its own line and then keeps writing
        must not have the next line silently captured as the query and
        stripped from the user-visible reply."""
        mock_status = AsyncMock()
        tool = ClinicalTrialsTool(mock_status)
        text = (
            "Let me search the registry.\n"
            "clinical_trials_query: pembrolizumab melanoma\n"
            "Also, here's what I think about the denial..."
        )
        match = tool.detect(text)
        self.assertIsNotNone(match)
        # Only "pembrolizumab melanoma" -- not the following sentence.
        self.assertEqual(match.group(1).strip(), "pembrolizumab melanoma")
        # The narrative after the tool call must survive clean_response.
        self.assertIn("Also, here's what I think", tool.clean_response(text, match))

    def _run(self, coro):
        return asyncio.run(coro)

    def test_execute_appends_trial_context_when_matches(self):
        """Happy path: matching token -> trials fetched -> context appended."""
        mock_status = AsyncMock()
        ct_tools = MagicMock()
        ct_tools.find_trials_for_query = AsyncMock(return_value=["NCT01234567"])

        trial = MagicMock()
        trial.nct_id = "NCT01234567"
        trial.brief_title = "Pembrolizumab in Melanoma"
        trial.overall_status = "RECRUITING"
        trial.phases = "PHASE3"
        trial.study_type = "INTERVENTIONAL"
        trial.conditions = "Melanoma"
        trial.interventions = "DRUG: Pembrolizumab"
        trial.has_results = False
        trial.start_date = "2023-01-01"
        trial.completion_date = "2026-01-01"
        trial.brief_summary = "Eval of pembrolizumab."
        trial.study_url = "https://clinicaltrials.gov/study/NCT01234567"
        ct_tools.get_trials = AsyncMock(return_value=[trial])
        ct_tools.format_trial_short = ClinicalTrialsTool(
            mock_status
        ).clinical_trials_tools.format_trial_short

        tool = ClinicalTrialsTool(mock_status, clinical_trials_tools=ct_tools)
        text = "**clinical_trials_query: pembrolizumab melanoma**"
        match = tool.detect(text)

        response, context = self._run(tool.execute(match, text, ""))

        self.assertNotIn("clinical_trials_query", response)
        self.assertIn("NCT01234567", context)
        self.assertIn("clinicaltrialscontext", context)
        ct_tools.find_trials_for_query.assert_awaited_once()
        ct_tools.get_trials.assert_awaited_once()

    def test_execute_returns_unchanged_context_when_no_matches(self):
        """Empty result: context stays untouched, response is cleaned."""
        mock_status = AsyncMock()
        ct_tools = MagicMock()
        ct_tools.find_trials_for_query = AsyncMock(return_value=[])
        ct_tools.get_trials = AsyncMock(return_value=[])

        tool = ClinicalTrialsTool(mock_status, clinical_trials_tools=ct_tools)
        text = "**clinical_trials_query: nonexistent xyz drug**"
        match = tool.detect(text)

        response, context = self._run(tool.execute(match, text, ""))

        self.assertNotIn("clinical_trials_query", response)
        self.assertEqual(context, "")
        ct_tools.find_trials_for_query.assert_awaited_once()
        ct_tools.get_trials.assert_not_awaited()


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

    def setUp(self):
        self.tool = MedicaidEligibilityTool(AsyncMock())

    def test_detect_medicaid_eligibility(self):
        """Test Medicaid eligibility tool detects pattern."""
        text = 'medicaid_eligibility {"income": 25000}'
        match = self.tool.detect(text)

        self.assertIsNotNone(match)
        self.assertIn("income", match.group(1))

    def test_detect_mid_prose_and_clean_keeps_preamble(self):
        """The pattern matches a call embedded in prose, and cleaning removes
        only the call — not the model's prose before it. (The pattern used to
        carry a leading `.*?`, which swallowed the preamble into group(0) and
        made every scan of a non-matching reply quadratic under DOTALL.)"""
        text = 'Let me check that. medicaid_eligibility {"income": 25000} Done.'
        matches = self.tool.detect_all(text)

        self.assertEqual(len(matches), 1)
        cleaned = self.tool.clean_all_matches(text, matches)
        self.assertIn("Let me check that.", cleaned)
        self.assertIn("Done.", cleaned)
        self.assertNotIn("medicaid_eligibility", cleaned)

    def test_build_eligibility_info_eligible(self):
        """Test eligibility info text generation for eligible user."""
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
        )

        self.assertIn("eligible for medicaid", info.lower())
        self.assertIn(f"current ({current_eligibility_year()})", info)

    def test_build_eligibility_info_with_missing(self):
        """Test eligibility info text when questions are missing."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=["income", "household_size"],
        )

        self.assertIn("questions to ask", info)

    def test_build_eligibility_info_lists_questions_not_python_repr(self):
        """Missing questions render as a readable list, not a list repr."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=["What state do you live in?", "How old are you?"],
        )

        self.assertIn("- What state do you live in?", info)
        self.assertNotIn("['What state", info)

    def test_build_eligibility_info_flags_experimental_when_asking(self):
        """The LLM is told to disclose the experimental status while asking."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=["What state do you live in?"],
        )

        self.assertIn("EXPERIMENTAL", info)

    def test_build_eligibility_info_flags_experimental_on_verdict(self):
        """The LLM is told to disclose the experimental status with verdicts."""
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
        )

        self.assertIn("EXPERIMENTAL", info)

    def test_build_eligibility_info_paces_questions(self):
        """The LLM is told to ask only a few questions at a time."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=["q1", "q2", "q3", "q4"],
        )

        self.assertIn("two or three at a time", info)

    def test_build_eligibility_info_reports_settled_verdict_while_asking(self):
        """A determination already reached isn't withheld behind a question."""
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=True,
            alternatives=[],
            missing=["Do you have ALS?"],
        )

        self.assertIn("already look eligible", info)
        self.assertIn("medicare", info.lower())

    def test_build_eligibility_info_holds_alternatives_until_verdict(self):
        """Denial-flavored next steps don't surface mid-interview."""
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=["If denied, you can appeal; gather documentation."],
            missing=["Do you have ALS?"],
        )

        self.assertNotIn("If denied", info)

    def test_indeterminate_result_is_not_rendered_as_a_denial(self):
        """ "Couldn't score them" must not become "may not be eligible"."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=["Contact your territory's Medicaid agency."],
            missing=[],
            determination_made=False,
        )

        self.assertIn("could NOT produce a Medicaid estimate", info)
        self.assertNotIn("may not be eligible", info)

    def test_indeterminate_result_still_reports_medicare(self):
        """Medicare is federal, so a territory resident still gets that answer."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=True,
            alternatives=[],
            missing=[],
            determination_made=False,
        )

        self.assertIn("may be eligible for it", info)

    def test_indeterminate_result_still_reports_firm_positive_verdicts(self):
        """A sub-check that DID finish with a yes is a real answer.

        Declining only the work-hours question (or the Medicare-side
        years-worked question) makes determination_made False, but the
        current-rules check may have completed with a firm positive --
        hiding it behind the blanket "no estimate" withheld a computed
        verdict.
        """
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=["We could not check qualifying work hours."],
            missing=[],
            determination_made=False,
        )

        self.assertIn("could NOT produce a Medicaid estimate", info)
        self.assertIn(f"current ({current_eligibility_year()})", info)
        self.assertIn("already look eligible", info)
        self.assertNotIn("may not be eligible", info)

    def test_indeterminate_result_reports_later_years_that_settled(self):
        """The later-year positives in a supplied timeline are reported too.

        Same rule as the missing-answers branch: only positives, since a year
        we could not score comes back False and drops out on that alone.
        """
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=True,
            medicare=False,
            alternatives=["We could not check qualifying work hours."],
            missing=[],
            determination_made=False,
            target_year=current + 1,
            timeline=[
                YearVerdict(current, False, []),
                YearVerdict(current + 1, True, []),
            ],
        )

        self.assertIn("could NOT produce a Medicaid estimate", info)
        self.assertIn(f"{current + 1}", info)
        self.assertIn("already look eligible", info)
        self.assertNotIn("may not be eligible", info)
        # A year past the published table earns the same caveat the full
        # verdict gives it: the positive above was scored with the published
        # year's income limits, not that year's.
        self.assertIn("newer income limits aren't published yet", info)

    def test_indeterminate_positive_for_the_published_year_needs_no_caveat(self):
        """The caveat is for years scored past the published table only."""
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=["We could not check qualifying work hours."],
            missing=[],
            determination_made=False,
            target_year=BASE_ELIGIBILITY_YEAR,
        )

        self.assertIn("already look eligible", info)
        self.assertNotIn("newer income limits aren't published yet", info)

    def test_scored_ineligible_still_reads_as_a_verdict(self):
        """A completed check that came back negative is a firm answer."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            determination_made=True,
        )

        self.assertIn("may not be eligible", info)

    def test_parsed_summary_echoes_recorded_values(self):
        """The LLM keeps its own context, so it must see what we recorded."""
        text = self.tool._build_parsed_summary(
            {
                "recorded": {"state": "ca"},
                "unreadable": [],
                "unrecognized": [],
                "declined": [],
            }
        )

        self.assertIn("state: ca", text)

    def test_parsed_summary_flags_unrecognized_parameters(self):
        """A silently dropped key looks to the model just like acceptance."""
        text = self.tool._build_parsed_summary(
            {
                "recorded": {},
                "unreadable": [],
                "unrecognized": ["income"],
                "declined": [],
            }
        )

        self.assertIn("income", text)
        self.assertIn("ignored", text)

    def test_parsed_summary_tells_llm_not_to_reask_declined_fields(self):
        text = self.tool._build_parsed_summary(
            {
                "recorded": {},
                "unreadable": [],
                "unrecognized": [],
                "declined": ["assets_total"],
            }
        )

        self.assertIn("don't ask about those again", text)

    def test_build_eligibility_info_lists_alternatives_not_python_repr(self):
        """Alternatives render as a readable list, not a list repr."""
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=["Consider CHIP for the kids."],
            missing=[],
        )

        self.assertIn("- Consider CHIP for the kids.", info)
        self.assertNotIn("['Consider", info)


class TestMedicaidTargetYear(TestCase):
    """The user picks which year the second verdict covers."""

    def setUp(self):
        self.tool = MedicaidEligibilityTool(AsyncMock())

    def test_verdict_names_the_requested_year(self):
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=2028,
        )

        self.assertIn("2028", info)
        self.assertNotIn("the 2026 rules", info)

    def test_the_table_year_target_is_reported_once(self):
        # Asking about the year we score against should not produce the same
        # verdict twice under two labels.
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=BASE_ELIGIBILITY_YEAR,
        )

        self.assertEqual(info.count("could be eligible for medicaid"), 1)

    def test_a_pre_work_requirement_year_gets_no_work_requirement_caveat(self):
        # Coaching someone to chase 80 hours a month for a rule that wasn't
        # in force in the year they asked about is noise at best.
        from fighthealthinsurance.medicaid_api import WORK_REQUIREMENT_FIRST_YEAR

        earlier = WORK_REQUIREMENT_FIRST_YEAR - 1
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=earlier,
            timeline=[YearVerdict(earlier, True, [])],
        )

        self.assertNotIn("work/community-engagement", info)

    def test_a_future_year_says_current_limits_were_used(self):
        # We do not model the later year's income limits, so the note has to
        # say what actually happened: the same published table scored both
        # years and the work requirement is the only difference.
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=2029,
        )

        self.assertIn("aren't published yet", info)
        self.assertIn("the work requirement, not the income test", info)

    def test_every_timeline_year_is_rendered(self):
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current + 3,
            timeline=[
                YearVerdict(current, True, []),
                YearVerdict(current + 1, False, []),
                YearVerdict(current + 3, False, []),
            ],
        )

        self.assertIn(f"current ({current}): they could be eligible", info)
        self.assertIn(f"{current + 1}: they may not be eligible", info)
        self.assertIn(f"{current + 3}: they may not be eligible", info)

    def test_a_change_shown_in_the_timeline_always_says_what_caused_it(self):
        # Asking about a pre-work-requirement year still renders the
        # work-requirement year beside it. Keying the caveats off the year the
        # user NAMED printed "this CHANGES in 2026" with nothing to say what
        # changed -- a flip with no stated cause.
        from fighthealthinsurance.medicaid_api import WORK_REQUIREMENT_FIRST_YEAR

        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=WORK_REQUIREMENT_FIRST_YEAR - 1,
            timeline=[
                YearVerdict(WORK_REQUIREMENT_FIRST_YEAR - 1, True, []),
                YearVerdict(WORK_REQUIREMENT_FIRST_YEAR, False, []),
            ],
        )

        self.assertIn(f"this CHANGES in {WORK_REQUIREMENT_FIRST_YEAR}", info)
        self.assertIn("work/community-engagement", info)

    def test_a_year_we_cannot_score_yet_is_not_reported_as_ineligible(self):
        # is_eligible returns False for BOTH "scored, falls short" and "can't
        # score until you answer" -- an unanswered work-hours question sets
        # the flag False on purpose. Rendering that as "may not be eligible"
        # hands the user a denial nobody computed.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current + 1,
            timeline=[
                YearVerdict(current, True, []),
                YearVerdict(
                    current + 1, False, ["About how many qualifying hours a month?"]
                ),
            ],
        )

        self.assertIn(f"{current + 1}: NOT ESTABLISHED", info)
        self.assertNotIn(f"{current + 1}: they may not be eligible", info)
        self.assertIn("About how many qualifying hours a month?", info)

    def test_an_unscored_year_does_not_trigger_a_change_callout(self):
        # "this CHANGES in <year>" off the back of an unanswered question is
        # the same uncomputed denial in a louder voice.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current + 1,
            timeline=[
                YearVerdict(current, True, []),
                YearVerdict(
                    current + 1, False, ["About how many qualifying hours a month?"]
                ),
            ],
        )

        self.assertNotIn("CHANGES in", info)

    def test_every_branch_tells_them_to_confirm_with_the_state(self):
        # Every answer is an estimate off simplified rules against limits that
        # move. The branch most likely to be taken as final -- "we couldn't
        # score you" -- is the one that used to end without any pointer at
        # all.
        from fighthealthinsurance.chat.tools.medicaid_tool import (
            CONFIRM_WITH_STATE_INSTRUCTION,
        )

        branches = {
            "mid-interview": dict(
                eligible_base=False,
                eligible_target=False,
                missing=["What is your household size?"],
                determination_made=True,
            ),
            "indeterminate": dict(
                eligible_base=False,
                eligible_target=False,
                missing=[],
                determination_made=False,
            ),
            "settled": dict(
                eligible_base=True,
                eligible_target=True,
                missing=[],
                determination_made=True,
            ),
        }
        for name, kwargs in branches.items():
            with self.subTest(branch=name):
                info = self.tool._build_eligibility_info(
                    medicare=False, alternatives=[], **kwargs
                )
                self.assertIn(CONFIRM_WITH_STATE_INSTRUCTION, info)

    def test_a_negative_verdict_carries_its_own_caveat(self):
        # The negative is the line someone acts on by NOT applying, so the
        # reminder rides on that line rather than waiting for a caveat
        # further down that the model may never reach.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current,
            timeline=[YearVerdict(current, False, [])],
        )

        line = next(
            row for row in info.splitlines() if row.startswith(f"- current ({current})")
        )
        self.assertIn("NOT a denial", line)
        self.assertIn("only their state can decide", line)

    def test_an_income_denial_does_not_blame_the_work_requirement(self):
        # Failing the income test is not the work requirement's doing.
        # Attaching "(once the 80-hours requirement applies...)" to an income
        # denial tells someone to go chase hours that would not have changed
        # the answer.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current,
            timeline=[YearVerdict(current, False, [])],
        )

        self.assertNotIn("work/community-engagement", info)

    def test_a_repeated_negative_does_not_repeat_the_whole_caveat(self):
        # Two rows carrying the same long sentence dilutes it.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current + 1,
            timeline=[
                YearVerdict(current, False, []),
                YearVerdict(current + 1, False, []),
            ],
        )

        self.assertEqual(info.count("people do qualify when a checker"), 1)
        self.assertIn("again, an estimate, not a denial", info)

    def test_a_transition_year_shortfall_is_conditional_not_a_denial(self):
        # The work requirement has reached the states that went early and
        # nowhere else yet. "May not be eligible" would be a denial for a
        # rule most states haven't adopted; a plain "could be" would hide
        # what's coming. The row has to say both.
        from fighthealthinsurance.medicaid_api import WORK_REQUIREMENT_UNIVERSAL_YEAR

        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current,
            timeline=[
                YearVerdict(current, True, [], work_requirement_conditional=True)
            ],
        )

        self.assertIn("could be eligible on income", info)
        self.assertIn("under 80 qualifying hours", info)
        self.assertIn("whether their state has already started", info)
        self.assertIn(f"January 1, {WORK_REQUIREMENT_UNIVERSAL_YEAR}", info)
        self.assertNotIn("they may not be eligible for medicaid", info)

    def test_a_conditional_row_does_not_swallow_the_work_requirement_note(self):
        # The shared explanation is attached once, to the first year the rule
        # bites. A conditional row spells it out itself -- if it consumed the
        # note on the way past, the year the rule actually bites was left
        # with no explanation at all.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current + 1,
            timeline=[
                YearVerdict(current, True, [], work_requirement_conditional=True),
                YearVerdict(current + 1, False, []),
            ],
        )

        self.assertIn(
            f"{current + 1}: they may not be eligible for medicaid (once the federal",
            info,
        )

    def test_a_conditional_year_does_not_trigger_a_change_callout(self):
        # A flip announced off a rule that may not have reached them is the
        # same uncomputed denial in a louder voice.
        current = current_eligibility_year()
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current + 1,
            timeline=[
                YearVerdict(current, True, [], work_requirement_conditional=True),
                YearVerdict(current + 1, False, []),
            ],
        )

        self.assertNotIn("CHANGES in", info)

    def test_a_finished_year_is_never_labelled_current(self):
        # The FPL table's year stops being "today" once the calendar passes
        # it. Labelling it "current" told people the rules they live under
        # were the ones that had just been replaced.
        current = current_eligibility_year()
        if BASE_ELIGIBILITY_YEAR >= current:
            self.skipTest("the published FPL table is still the current year")

        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current,
            timeline=[
                YearVerdict(BASE_ELIGIBILITY_YEAR, True, []),
                YearVerdict(current, True, []),
            ],
        )

        self.assertIn(f"current ({current})", info)
        self.assertNotIn(f"current ({BASE_ELIGIBILITY_YEAR})", info)
        self.assertIn(f"{BASE_ELIGIBILITY_YEAR}: they could be eligible", info)

    def test_a_finished_base_year_is_dropped_when_no_timeline_is_given(self):
        # Direct callers that pass no timeline used to get a row for the FPL
        # table's year regardless of whether it had already ended.
        current = current_eligibility_year()
        if BASE_ELIGIBILITY_YEAR >= current:
            self.skipTest("the published FPL table is still the current year")

        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=current,
        )

        self.assertNotIn(f"{BASE_ELIGIBILITY_YEAR}: they", info)
        self.assertIn(f"current ({current}): they may not be eligible", info)

    def test_a_year_over_year_change_is_called_out(self):
        # The reason for showing more than one year at all: don't leave the
        # user to diff two sentences.
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=2026,
            timeline=[YearVerdict(2025, True, []), YearVerdict(2026, False, [])],
        )

        self.assertIn("this CHANGES in 2026", info)
        self.assertIn("probably would NOT from 2026 on", info)

    def test_an_improvement_is_called_out_too(self):
        info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=2026,
            timeline=[YearVerdict(2025, False, []), YearVerdict(2026, True, [])],
        )

        self.assertIn("IMPROVES in 2026", info)

    def test_a_steady_timeline_has_no_change_callout(self):
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=2026,
            timeline=[YearVerdict(2025, True, []), YearVerdict(2026, True, [])],
        )

        self.assertNotIn("CHANGES in", info)
        self.assertNotIn("IMPROVES in", info)

    def test_the_verdicts_stay_hedged(self):
        # "Keeping the probably words going" -- none of these are
        # determinations and the wording must not harden into one.
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=2026,
            timeline=[YearVerdict(2025, True, []), YearVerdict(2026, False, [])],
        )

        self.assertIn("an approximation, not a determination", info)
        self.assertIn("could be eligible", info)
        self.assertIn("may not be eligible", info)
        self.assertIn("probably", info)

    def test_a_base_year_check_has_no_second_year_note(self):
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=[],
            target_year=BASE_ELIGIBILITY_YEAR,
        )

        self.assertNotIn("aren't published yet", info)

    def test_target_year_positive_is_reported_mid_interview(self):
        # The base-year positive was already shared while questions were
        # outstanding; the target-year one sat unsaid behind them, which is
        # the half someone asking "will I still qualify in 2029?" wanted.
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=[],
            missing=["What is your household size?"],
            target_year=2029,
        )

        self.assertIn("already look eligible in 2029", info)

    def test_default_target_year_still_covers_the_work_requirement(self):
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=False,
            medicare=False,
            alternatives=[],
            missing=[],
        )

        self.assertIn(str(DEFAULT_TARGET_YEAR), info)
        self.assertIn("work/community-engagement", info)


class TestMedicaidInfoStartsTheEligibilityPath(TestCase):
    """A general Medicaid answer has to lead somewhere.

    Someone who asks how Medicaid works in their state is usually trying to
    find out whether they can get covered, so the info lookup hands the model
    the next step instead of ending at a phone number.
    """

    def setUp(self):
        self.handoff = MedicaidInfoTool._build_eligibility_handoff("California")

    def test_handoff_names_the_eligibility_tool(self):
        self.assertIn("medicaid_eligibility", self.handoff)

    def test_handoff_carries_the_state_we_just_looked_up(self):
        self.assertIn("California", self.handoff)

    def test_handoff_calls_the_check_experimental(self):
        self.assertIn("EXPERIMENTAL", self.handoff)

    def test_handoff_forbids_questions_before_the_tool_call(self):
        self.assertIn("never ask eligibility questions before", self.handoff)

    def test_handoff_offers_rather_than_forces_the_check(self):
        # Someone who asked what Medicaid is may just want that answered;
        # the handoff has to read as an invitation, not a redirect.
        self.assertIn("Offer it, don't push it", self.handoff)
        self.assertIn("drop it if they decline", self.handoff)

    def test_handoff_omits_the_state_line_when_none_was_given(self):
        # The old handoff pasted the state into a literal JSON tool call, so
        # a lookup with no "state" key emitted **medicaid_eligibility
        # {"state": "the state"}** -- a made-up value the model then re-sent.
        handoff = MedicaidInfoTool._build_eligibility_handoff(None)
        self.assertNotIn("You already know their state", handoff)
        self.assertNotIn("the state", handoff)

    def test_handoff_does_not_restart_a_check_already_underway(self):
        self.assertIn("already underway", self.handoff)

    def test_lookup_result_carries_the_handoff(self):
        # The guidance is only useful if it actually rides along with the
        # info the model is answering from.
        tool = MedicaidInfoTool(AsyncMock())
        match = tool.detect('**medicaid_info {"state": "California"}**')
        self.assertIsNotNone(match)

        with patch(
            "fighthealthinsurance.medicaid_api.get_medicaid_info",
            return_value="Call 1-800-555-0100 to apply.",
        ):
            captured = {}

            async def fake_call_llm(model_backends, message, *args, **kwargs):
                captured["message"] = message
                return ("ok", "")

            tool.call_llm_callback = fake_call_llm
            asyncio.run(
                tool.execute(
                    match,
                    '**medicaid_info {"state": "California"}**',
                    "",
                    model_backends=["backend"],
                    current_message_for_llm="How does Medi-Cal work?",
                    history_for_llm=[],
                )
            )

        self.assertIn("medicaid_eligibility", captured["message"])


class TestEligibilityVerifiedFlag(TestCase):
    """Running the checker is not the same as the checker reaching a verdict.

    The flag exempts a reply from the invented-verdict penalty, so it must
    only be set once there is a real determination to relay. Setting it on
    every call handed out the exemption in exactly the cases the guard exists
    for -- a mid-interview model jumping to "you don't qualify", or one
    ignoring "we could NOT produce an estimate for this person".
    """

    def _run(self, verdict):
        """Execute the tool with a stubbed checker, return (flag, kwargs)."""
        computed = [False]
        tool = MedicaidEligibilityTool(AsyncMock(), eligibility_computed=computed)
        call = '**medicaid_eligibility {"state": "CA"}**'
        match = tool.detect(call)
        self.assertIsNotNone(match)

        captured: dict = {}

        async def fake_call_llm(model_backends, message, *args, **kwargs):
            captured.update(kwargs)
            return ("ok", "")

        tool.call_llm_callback = fake_call_llm
        with patch(
            "fighthealthinsurance.medicaid_api.is_eligible", return_value=verdict
        ):
            asyncio.run(
                tool.execute(
                    match,
                    call,
                    "",
                    model_backends=["backend"],
                    current_message_for_llm="Am I eligible?",
                    history_for_llm=[],
                )
            )
        return computed[0], captured

    def test_final_determination_earns_the_exemption(self):
        flag, kwargs = self._run((True, True, False, [], [], True))
        self.assertTrue(flag)
        self.assertTrue(kwargs["eligibility_verified"])

    def test_final_ineligible_determination_earns_the_exemption(self):
        # A computed "no" is still a verdict the model may relay.
        flag, kwargs = self._run(
            (False, False, False, ["Try the marketplace"], [], True)
        )
        self.assertTrue(flag)
        self.assertTrue(kwargs["eligibility_verified"])

    def test_mid_interview_with_nothing_settled_does_not(self):
        flag, kwargs = self._run(
            (False, False, False, [], ["What is your income?"], True)
        )
        self.assertFalse(flag)
        self.assertFalse(kwargs["eligibility_verified"])

    def test_mid_interview_positive_does_earn_the_exemption(self):
        # The checker reports an already-settled positive while questions are
        # outstanding, and _build_eligibility_info tells the model to share
        # it -- penalizing that would fight our own tool output.
        flag, kwargs = self._run(
            (True, False, False, [], ["What is your income?"], True)
        )
        self.assertTrue(flag)
        self.assertTrue(kwargs["eligibility_verified"])

    def test_medicare_only_positive_does_not_earn_the_exemption(self):
        # A 67-year-old's age settles Medicare on the very first call while
        # every Medicaid question is still outstanding. The exemption is
        # program-blind, so latching it there let the rest of the session
        # assert uncomputed MEDICAID verdicts for free.
        flag, kwargs = self._run(
            (False, False, True, [], ["What is your monthly income?"], True)
        )
        self.assertFalse(flag)
        self.assertFalse(kwargs["eligibility_verified"])

    def test_unscoreable_person_does_not_earn_the_exemption(self):
        # determination_made=False: a territory, or a declined required
        # answer. The info text explicitly says not to call them ineligible.
        flag, kwargs = self._run(
            (False, False, False, ["Contact your territory's agency"], [], False)
        )
        self.assertFalse(flag)
        self.assertFalse(kwargs["eligibility_verified"])


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
        return asyncio.run(coro)

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

    def test_strips_userinfo(self):
        """Test that userinfo (credentials) is stripped from netloc."""
        url = "https://user:pass@example.com/doc.pdf"
        sanitized = _sanitize_url_for_display(url)
        self.assertNotIn("user", sanitized)
        self.assertNotIn("pass", sanitized)
        self.assertIn("example.com", sanitized)


class TestDocFetcherRateLimit(TestCase):
    """Test rate limiting in DocFetcherTool."""

    def _run(self, coro):
        return asyncio.run(coro)

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
        _response, _context = self._run(tool.execute(match, "response text", "context"))

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
            _response, _context = self._run(
                tool.execute(match, "response text", "context")
            )

        # Counter should have incremented even though fetch failed
        self.assertEqual(fetch_count[0], 1)


class TestDocFetcherExecute(TestCase):
    """Test DocFetcherTool.execute end-to-end with mocked fetcher."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_invalid_json_returns_cleanly(self):
        """Test that invalid JSON is handled gracefully."""
        mock_status = AsyncMock()
        tool = DocFetcherTool(mock_status)
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock()

        tool_call = "**fetch_doc {not valid json}**"
        match = re.search(FETCH_DOC_REGEX, tool_call, re.IGNORECASE)
        self.assertIsNotNone(match)
        response, _context = self._run(
            tool.execute(match, f"Here is info. {tool_call} More text.", "ctx")
        )
        # Fetcher should not have been called
        tool.fetcher.fetch_and_extract_text.assert_not_called()
        # Response should have the tool call stripped
        self.assertNotIn("fetch_doc", response)
        self.assertIn("Here is info.", response)

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
        _response, _context = self._run(tool.execute(match, "original", "ctx"))
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


class TestUSPSTFLookupPattern(TestCase):
    """Pattern matching for USPSTF_LOOKUP_REGEX."""

    def test_matches_with_stars(self):
        text = '**uspstf_lookup {"query": "colon cancer", "grade": "A"}**'
        match = re.search(USPSTF_LOOKUP_REGEX, text, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn("colon cancer", match.group(1))

    def test_matches_without_stars(self):
        text = 'uspstf_lookup {"query": "breast"}'
        match = re.search(USPSTF_LOOKUP_REGEX, text, re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("breast", match.group(1))

    def test_does_not_match_unrelated(self):
        text = "Please look up USPSTF guidance about screening."
        match = re.search(USPSTF_LOOKUP_REGEX, text, re.IGNORECASE)
        self.assertIsNone(match)


class TestUSPSTFLookupTool(TestCase):
    """USPSTFLookupTool detection and execute."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_detect(self):
        mock_status = AsyncMock()
        tool = USPSTFLookupTool(mock_status)

        text = '**uspstf_lookup {"query": "colon cancer"}**'
        match = tool.detect(text)
        self.assertIsNotNone(match)

    def test_detect_all_finds_multiple(self):
        mock_status = AsyncMock()
        tool = USPSTFLookupTool(mock_status)

        text = (
            '**uspstf_lookup {"query": "colon"}** and '
            '**uspstf_lookup {"query": "lung"}**'
        )
        matches = tool.detect_all(text)
        self.assertEqual(len(matches), 2)

    def test_execute_with_invalid_json_returns_friendly_error(self):
        mock_status = AsyncMock()
        tool = USPSTFLookupTool(mock_status)
        text = "**uspstf_lookup {invalid json}**"
        match = re.search(USPSTF_LOOKUP_REGEX, text, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(match)

        response, context = self._run(tool.execute(match, text, ""))
        self.assertIn("uspstf", response.lower())
        self.assertEqual(context, "")

    def test_execute_appends_uspstf_info_to_context(self):
        mock_status = AsyncMock()
        tool = USPSTFLookupTool(mock_status)

        text = '**uspstf_lookup {"query": "colorectal", "limit": 1}**'
        match = re.search(USPSTF_LOOKUP_REGEX, text, re.IGNORECASE | re.DOTALL)

        with patch(
            "fighthealthinsurance.uspstf_api.get_uspstf_info",
            return_value="USPSTF: Colorectal screening Grade A.",
        ):
            response, context = self._run(tool.execute(match, text, ""))

        self.assertIn("Colorectal screening", context)
        self.assertNotIn("uspstf_lookup", response)

    def test_execute_with_callback_invokes_llm(self):
        mock_status = AsyncMock()
        callback = AsyncMock(return_value=("LLM expanded reply", "summary"))
        tool = USPSTFLookupTool(mock_status, call_llm_callback=callback)

        text = '**uspstf_lookup {"query": "diabetes"}**'
        match = re.search(USPSTF_LOOKUP_REGEX, text, re.IGNORECASE | re.DOTALL)

        with patch(
            "fighthealthinsurance.uspstf_api.get_uspstf_info",
            return_value="USPSTF: Diabetes Grade B.",
        ):
            response, context = self._run(
                tool.execute(
                    match,
                    text,
                    "",
                    model_backends=[MagicMock()],
                    history_for_llm=[],
                )
            )

        self.assertEqual(callback.call_count, 1)
        # The follow-up LLM reply should be appended to the cleaned response.
        self.assertIn("LLM expanded reply", response)
        # Raw lookup is also included in the context.
        self.assertIn("Diabetes", context)
        # ``previous_context_summary`` (3rd positional arg) must identify the
        # lookup so the LLM follow-up call gets the correct provenance label.
        call_args = callback.call_args
        self.assertEqual(call_args.args[2], "USPSTF preventive-services lookup")


class TestMedicaidDeclinedAnswerRoundTrip(TestCase):
    """A declined answer has to survive into the next tool call.

    ``declined_set`` is rebuilt from the current payload each call, so the
    "unknown" marker only suppresses its question for as long as the model
    keeps re-sending it. The parsed-input echo is the only place the model is
    told what to re-send, so it has to name the declined fields explicitly.
    """

    def setUp(self):
        self.tool = MedicaidEligibilityTool(AsyncMock())

    def test_summary_tells_the_model_to_resend_declined_fields(self):
        summary = summarize_eligibility_inputs(
            {"state": "ca", "age": 66, "assets_total": "unknown"}
        )
        text = self.tool._build_parsed_summary(summary)
        self.assertIn("assets_total", text)
        self.assertIn('"unknown"', text)

    def test_resending_the_unknown_marker_keeps_the_question_suppressed(self):
        first = dict(
            state="ca",
            age=66,
            married=False,
            household_size=1,
            monthly_income=900,
            children_in_household=0,
            pregnant=False,
            receiving_ssdi=False,
            on_medicare=False,
            years_worked=40,
            assets_total="unknown",
        )
        # What the echo tells the model to send back, plus the declined marker
        # the echo now names separately.
        summary = summarize_eligibility_inputs(first)
        second = dict(summary["recorded"])
        for field in summary["declined"]:
            second[field] = "unknown"

        *_, missing, _ = is_eligible(**second)
        self.assertEqual(missing, [])


class TestMedicaidIndeterminateWithQuestions(TestCase):
    """An unscorable result's next steps are actionable before the interview ends."""

    def setUp(self):
        self.tool = MedicaidEligibilityTool(AsyncMock())
        # An unscorable result that still has an outstanding question -- the
        # territory case, where Medicare may yet resolve.
        self.indeterminate_info = self.tool._build_eligibility_info(
            eligible_base=False,
            eligible_target=False,
            medicare=False,
            alternatives=["Contact your territory's Medicaid agency."],
            missing=["How old are you?"],
            determination_made=False,
        )

    def test_next_steps_are_shared_while_questions_remain(self):
        self.assertIn(
            "Contact your territory's Medicaid agency.", self.indeterminate_info
        )

    def test_no_ineligibility_claim_while_questions_remain(self):
        self.assertNotIn("may not be eligible", self.indeterminate_info)

    def test_a_scored_result_does_not_get_the_cannot_estimate_banner(self):
        info = self.tool._build_eligibility_info(
            eligible_base=True,
            eligible_target=True,
            medicare=False,
            alternatives=["Visit your state Medicaid page."],
            missing=["How old are you?"],
            determination_made=True,
        )
        self.assertNotIn("canNOT produce a Medicaid estimate", info)

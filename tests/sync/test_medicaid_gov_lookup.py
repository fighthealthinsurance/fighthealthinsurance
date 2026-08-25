"""Tests for Medicaid.gov page lookup (curated pages + sitemap search)."""

import asyncio
from unittest.mock import AsyncMock, patch

from django.core.cache import cache
from django.test import TestCase

from fighthealthinsurance.chat.tools import MedicaidGovLookupTool
from fighthealthinsurance.medicaid_gov_api import (
    CURATED_SOURCES,
    is_allowed_url,
    resolve_curated_source,
    search_medicaid_gov,
    suggest_curated_sources,
)


class TestCuratedSources(TestCase):
    """Named pages, so the model asks for "renew_info" not a 130-char URL."""

    def test_national_renewal_page(self):
        self.assertEqual(
            resolve_curated_source("renew_info"),
            "https://www.medicaid.gov/renew-info",
        )

    def test_per_state_renewal_page(self):
        self.assertEqual(
            resolve_curated_source("renew_info", "Iowa"),
            "https://www.medicaid.gov/renew-info/ia/",
        )

    def test_state_program_names_resolve(self):
        # The model shouldn't have to convert "Medi-Cal" to "ca" itself.
        self.assertEqual(
            resolve_curated_source("renew_info", "Medi-Cal"),
            "https://www.medicaid.gov/renew-info/ca/",
        )

    def test_an_unreadable_state_falls_back_to_the_national_page(self):
        # Better than guessing a slug that would 404.
        self.assertEqual(
            resolve_curated_source("renew_info", "Atlantis"),
            "https://www.medicaid.gov/renew-info",
        )

    def test_a_state_on_a_page_without_state_variants_is_ignored(self):
        self.assertEqual(
            resolve_curated_source("eligibility_levels", "Iowa"),
            CURATED_SOURCES["eligibility_levels"].url,
        )

    def test_hyphenated_and_cased_page_names_are_accepted(self):
        self.assertEqual(
            resolve_curated_source("Renew-Info"),
            "https://www.medicaid.gov/renew-info",
        )

    def test_unknown_page_returns_none(self):
        self.assertIsNone(resolve_curated_source("not_a_page"))
        self.assertIsNone(resolve_curated_source(""))


class TestAllowlist(TestCase):
    """The tool must never become a general-purpose fetcher."""

    def test_allowed_reference_hosts(self):
        for url in (
            "https://www.medicaid.gov/eligibility",
            "https://www.coveredca.com/pdfs/FPL-chart.pdf",
            "https://www.healthinsurance.org/glossary/federal-poverty-level/",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_allowed_url(url))

    def test_other_hosts_are_refused(self):
        self.assertFalse(is_allowed_url("https://evil.example.com/x"))
        self.assertFalse(is_allowed_url("https://medicaid.gov.evil.example.com/x"))

    def test_non_http_schemes_are_refused(self):
        self.assertFalse(is_allowed_url("file:///etc/passwd"))
        self.assertFalse(is_allowed_url(""))

    def test_robots_disallowed_paths_are_refused(self):
        # medicaid.gov's robots.txt asks crawlers to stay out of these.
        for path in ("/search/content", "/admin/config", "/node/123", "/user/login"):
            with self.subTest(path=path):
                self.assertFalse(is_allowed_url(f"https://www.medicaid.gov{path}"))


class TestCuratedSuggestions(TestCase):
    """The sitemap carries no page titles, so phrasing needs its own routing."""

    def test_renewal_phrasings_reach_the_renewal_hub(self):
        for query in (
            "renewal paperwork",
            "my coverage is ending",
            "I got disenrolled",
            "redetermination notice",
        ):
            with self.subTest(query=query):
                keys = [s.key for s in suggest_curated_sources(query)]
                self.assertIn("renew_info", keys)

    def test_income_phrasings_reach_the_eligibility_table(self):
        keys = [s.key for s in suggest_curated_sources("what income limits apply")]
        self.assertIn("eligibility_levels", keys)

    def test_poverty_level_phrasings_reach_an_fpl_reference(self):
        keys = [s.key for s in suggest_curated_sources("what is the federal poverty level")]
        self.assertIn("fpl_glossary", keys)

    def test_an_unrelated_question_suggests_nothing(self):
        self.assertEqual(suggest_curated_sources("who won the world series"), [])


class TestSitemapSearch(TestCase):
    """Slug matching over the sitemap stands in for the site's own search.

    medicaid.gov runs a credentialed Vertex AI Search widget and disallows
    /search/ in robots.txt, so the sitemap is the sanctioned index.
    """

    SITEMAP = [
        "https://www.medicaid.gov/renew-info",
        "https://www.medicaid.gov/eligibility",
        "https://www.medicaid.gov/medicaid/eligibility-policy",
        "https://www.medicaid.gov/chip/state-program-information/chip-spa",
    ]

    def setUp(self):
        cache.clear()
        patcher = patch(
            "fighthealthinsurance.medicaid_gov_api._fetch_sitemap_urls",
            return_value=self.SITEMAP,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ranks_matching_pages(self):
        results = search_medicaid_gov("eligibility policy")
        self.assertTrue(results)
        self.assertEqual(results[0][0], "https://www.medicaid.gov/medicaid/eligibility-policy")

    def test_word_variants_match_slugs(self):
        # "renewal" has to reach "/renew-info"; slugs and questions rarely
        # agree on suffixes.
        results = search_medicaid_gov("renewal")
        self.assertEqual(results[0][0], "https://www.medicaid.gov/renew-info")

    def test_generic_words_alone_match_nothing(self):
        # Every slug contains "medicaid"; matching on it ranks the whole site.
        self.assertEqual(search_medicaid_gov("medicaid"), [])

    def test_empty_query_matches_nothing(self):
        self.assertEqual(search_medicaid_gov(""), [])
        self.assertEqual(search_medicaid_gov("  "), [])

    def test_limit_is_respected(self):
        self.assertLessEqual(len(search_medicaid_gov("eligibility", limit=1)), 1)


def _run_tool(params_json, *, lookup_count=None, fetch_text="Official page text."):
    """Execute one medicaid_gov_lookup call, returning the LLM-facing message."""
    tool = MedicaidGovLookupTool(AsyncMock(), lookup_count=lookup_count)
    call = f"**medicaid_gov_lookup {params_json}**"
    match = tool.detect(call)
    assert match is not None, f"pattern did not match {call}"

    captured = {}

    async def fake_llm(model_backends, message, *args, **kwargs):
        captured["message"] = message
        return ("ok", "")

    tool.call_llm_callback = fake_llm

    async def fake_fetch(url, **kwargs):
        captured["url"] = url
        return fetch_text, "html"

    with patch.object(tool.fetcher, "fetch_and_extract_text", side_effect=fake_fetch):
        _, context = asyncio.run(
            tool.execute(
                match,
                call,
                "",
                model_backends=["backend"],
                current_message_for_llm="When do I have to renew?",
            )
        )
    return captured, context


class TestMedicaidGovLookupTool(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_curated_page_is_fetched_and_handed_to_the_model(self):
        captured, context = _run_tool('{"page": "renew_info", "state": "IA"}')

        self.assertEqual(captured["url"], "https://www.medicaid.gov/renew-info/ia/")
        self.assertIn("Official page text.", captured["message"])
        self.assertIn("renew-info/ia", context)

    def test_the_users_question_rides_along(self):
        captured, _ = _run_tool('{"page": "renew_info"}')
        self.assertIn("When do I have to renew?", captured["message"])

    def test_an_off_allowlist_url_is_never_fetched(self):
        captured, _ = _run_tool('{"url": "https://evil.example.com/x"}')
        self.assertNotIn("url", captured)

    def test_an_allowlisted_url_is_fetched(self):
        captured, _ = _run_tool('{"url": "https://www.medicaid.gov/eligibility"}')
        self.assertEqual(captured["url"], "https://www.medicaid.gov/eligibility")

    def test_an_unknown_page_returns_the_menu_instead_of_failing_silently(self):
        captured, context = _run_tool('{"page": "does_not_exist"}')

        self.assertNotIn("url", captured)
        self.assertIn("renew_info", context)
        self.assertIn("eligibility_levels", context)

    def test_the_session_cap_is_enforced(self):
        from fighthealthinsurance.chat.tools.medicaid_gov_tool import (
            MAX_LOOKUPS_PER_SESSION,
        )

        count = [MAX_LOOKUPS_PER_SESSION]
        captured, _ = _run_tool('{"page": "renew_info"}', lookup_count=count)

        self.assertNotIn("url", captured)

    def test_malformed_json_is_ignored(self):
        tool = MedicaidGovLookupTool(AsyncMock())
        call = '**medicaid_gov_lookup {"page": }**'
        match = tool.detect(call)
        self.assertIsNotNone(match)
        cleaned, context = asyncio.run(tool.execute(match, call, "seed"))
        self.assertEqual(context, "seed")

    def test_an_empty_page_is_not_passed_off_as_an_answer(self):
        captured, context = _run_tool('{"page": "renew_info"}', fetch_text="   ")
        self.assertNotIn("message", captured)
        self.assertEqual(context, "")

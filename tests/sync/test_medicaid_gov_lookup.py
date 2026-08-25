"""Tests for Medicaid.gov page lookup (curated pages + sitemap search)."""

import asyncio
from unittest.mock import AsyncMock, patch

from django.core.cache import cache
from django.test import TestCase

from fighthealthinsurance.chat.tools import MedicaidGovLookupTool
from fighthealthinsurance.medicaid_gov_api import (
    CURATED_SOURCES,
    _parse_sitemap_xml,
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

    def test_cleartext_http_is_refused(self):
        # These pages are the authority we quote to people about their
        # coverage; over cleartext anything on the path can rewrite the
        # answer. Every curated source is https.
        self.assertFalse(is_allowed_url("http://www.medicaid.gov/renew-info"))
        self.assertTrue(is_allowed_url("https://www.medicaid.gov/renew-info"))

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

    def test_work_requirement_phrasings_reach_the_community_engagement_hub(self):
        # "Does the work requirement apply to me yet?" is a question CMS
        # answers with a rule, not a state list -- there is no published list
        # of which states opted in early, so this page plus the person's own
        # state agency is the whole of the available answer.
        for query in (
            "do i have to work to keep medicaid",
            "what are the 80 hours rules",
            "medicaid work requirements",
            "how do qualifying hours work",
        ):
            with self.subTest(query=query):
                suggestions = suggest_curated_sources(query)
                self.assertTrue(suggestions)
                self.assertEqual(suggestions[0].key, "community_engagement")

    def test_the_community_engagement_hub_is_an_allowed_medicaid_gov_page(self):
        url = resolve_curated_source("community_engagement")

        self.assertIsNotNone(url)
        self.assertTrue(is_allowed_url(url))
        self.assertIn("community-engagement", url)

    def test_income_phrasings_reach_the_eligibility_table(self):
        keys = [s.key for s in suggest_curated_sources("what income limits apply")]
        self.assertIn("eligibility_levels", keys)

    def test_poverty_level_phrasings_reach_an_fpl_reference(self):
        keys = [
            s.key for s in suggest_curated_sources("what is the federal poverty level")
        ]
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
        self.assertEqual(
            results[0][0], "https://www.medicaid.gov/medicaid/eligibility-policy"
        )

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

    def test_a_zero_limit_returns_nothing(self):
        # "at most N results" has to mean it at zero too.
        self.assertEqual(search_medicaid_gov("eligibility", limit=0), [])


class TestSitemapParsing(TestCase):
    """The sitemap is remote content we don't control, so parse it warily."""

    def test_a_plain_sitemap_parses(self):
        xml = (
            b'<?xml version="1.0"?>'
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://www.medicaid.gov/renew-info</loc></url>"
            b"</urlset>"
        )
        self.assertIsNotNone(_parse_sitemap_xml(xml))

    def test_a_doctype_declaration_is_refused(self):
        # Sitemaps have no DTD, so one here is a mistake or an
        # entity-expansion attempt.
        xml = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE urlset [<!ENTITY lol "lol">]>'
            b"<urlset><url><loc>&lol;</loc></url></urlset>"
        )
        with self.assertRaises(ValueError):
            _parse_sitemap_xml(xml)

    def test_a_doctype_after_a_long_comment_is_still_refused(self):
        # A fixed inspection window is a guard you can walk around with
        # padding: a long enough leading comment pushes the declaration past
        # any prefix scan.
        xml = (
            b'<?xml version="1.0"?>'
            b"<!--" + b"x" * 4096 + b"-->"
            b'<!DOCTYPE urlset [<!ENTITY v "renew-info">]>'
            b"<urlset><url><loc>&v;</loc></url></urlset>"
        )
        with self.assertRaises(ValueError):
            _parse_sitemap_xml(xml)

    def test_a_utf16_declaration_is_refused(self):
        # A document declares its own encoding and the parser honours it, so
        # in UTF-16 the marker arrives as b"<\x00!\x00D\x00..." and sails
        # past a plain byte search -- the guard looks like it holds while the
        # document parses with its entities intact.
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                xml = (
                    '<?xml version="1.0" encoding="UTF-16"?>'
                    '<!DOCTYPE urlset [<!ENTITY v "renew-info">]>'
                    "<urlset><url><loc>&v;</loc></url></urlset>"
                ).encode(encoding)
                with self.assertRaises(ValueError):
                    _parse_sitemap_xml(xml)

    def test_a_plain_utf16_sitemap_still_parses(self):
        # The encoding itself isn't the problem -- only a declaration in it.
        xml = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://www.medicaid.gov/renew-info</loc></url>"
            "</urlset>"
        ).encode("utf-16")

        self.assertIsNotNone(_parse_sitemap_xml(xml))

    def test_an_oversized_document_is_refused(self):
        from fighthealthinsurance.medicaid_gov_api import _MAX_SITEMAP_BYTES

        with self.assertRaises(ValueError):
            _parse_sitemap_xml(b"<urlset/>" + b" " * (_MAX_SITEMAP_BYTES + 1))


class TestSitemapWalkBudget(TestCase):
    """A slow upstream should cost us the lookup, not the whole chat turn."""

    INDEX = (
        b'<?xml version="1.0"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://www.medicaid.gov/sitemap-1.xml</loc></sitemap>"
        b"</sitemapindex>"
    )

    def setUp(self):
        cache.clear()

    def test_the_walk_stops_once_the_budget_is_spent(self):
        from fighthealthinsurance import medicaid_gov_api

        responses = []

        class _Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            responses.append(url)
            return _Response(self.INDEX)

        with patch.object(medicaid_gov_api, "_SITEMAP_TOTAL_BUDGET_SECONDS", 0):
            with patch.object(medicaid_gov_api.requests, "get", side_effect=fake_get):
                urls = medicaid_gov_api._fetch_sitemap_urls()

        # No budget, no requests -- not even the index. A per-request timeout
        # alone doesn't bound the walk: a request that starts a hair before
        # the deadline still runs the full timeout past it.
        self.assertEqual(responses, [])
        self.assertEqual(urls, [])

    def test_an_off_host_sitemap_entry_is_never_requested(self):
        # Child sitemap URLs come out of remote XML. Without a host check a
        # spoofed index turns one lookup into a request at whatever host it
        # names.
        from fighthealthinsurance import medicaid_gov_api

        evil_index = (
            b'<?xml version="1.0"?>'
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<sitemap><loc>https://evil.example.com/sitemap-1.xml</loc></sitemap>"
            b"</sitemapindex>"
        )
        responses = []

        class _Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            responses.append(url)
            return _Response(evil_index)

        with patch.object(medicaid_gov_api.requests, "get", side_effect=fake_get):
            urls = medicaid_gov_api._fetch_sitemap_urls()

        self.assertEqual(responses, [medicaid_gov_api.SITEMAP_INDEX_URL])
        self.assertEqual(urls, [])

    def test_redirects_are_not_followed(self):
        # The host check only means something if we choose what we connect
        # to; a 302 would hand that choice back to whoever served the sitemap.
        from fighthealthinsurance import medicaid_gov_api

        seen = {}

        class _Response:
            content = self.INDEX

            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            seen.update(kwargs)
            return _Response()

        with patch.object(medicaid_gov_api.requests, "get", side_effect=fake_get):
            medicaid_gov_api._fetch_sitemap_urls()

        self.assertFalse(seen.get("allow_redirects", True))


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

    def test_a_capped_session_does_no_network_work_at_all(self):
        # Resolving a {"query": ...} call walks the sitemap over the network.
        # A session that has spent its allowance must not pay for a lookup it
        # cannot use.
        from fighthealthinsurance.chat.tools.medicaid_gov_tool import (
            MAX_LOOKUPS_PER_SESSION,
        )

        with patch(
            "fighthealthinsurance.medicaid_gov_api._fetch_sitemap_urls",
            return_value=[],
        ) as sitemap:
            _run_tool(
                '{"query": "eligibility policy"}',
                lookup_count=[MAX_LOOKUPS_PER_SESSION],
            )

        sitemap.assert_not_called()

    def test_a_failed_resolution_does_not_spend_the_budget(self):
        # A bad page name is a recoverable mistake -- the model gets the menu
        # back and can try again.
        count = [0]
        _run_tool('{"page": "does_not_exist"}', lookup_count=count)

        self.assertEqual(count, [0])

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

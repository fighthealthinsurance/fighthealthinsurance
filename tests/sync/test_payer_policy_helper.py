"""Tests for payer_policy_helper, payer_policy_parsers, and the
ingest_payer_policy_indexes management command."""

import asyncio
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from fighthealthinsurance.models import InsuranceCompany, PayerPolicyEntry
from fighthealthinsurance.payer_policy_fetcher import PayerPolicyFetcher
from fighthealthinsurance.payer_policy_helper import (
    COMPARATIVE_PAYER_CAVEAT,
    NO_ENTRIES_GUARD,
    WITH_ENTRIES_GUARD,
    _format_company_block,
    _keyword_terms,
    get_combined_payer_policy_context,
    get_comparative_payer_policy_context,
    get_payer_policy_context,
    get_relevant_policy_entries,
    resolve_company_from_text,
)
from fighthealthinsurance.payer_policy_parsers import (
    PARSERS_BY_HOST,
    parse_cigna_medical_a_z,
)

# Sample of real Cigna A-Z markup (titles + relative PDF hrefs).
CIGNA_SAMPLE_HTML = """
<html><body>
<a href="/assets/chcp/pdf/coveragePolicies/medical/cpg024_acupuncture.pdf">
  Acupuncture - (CPG024)
</a>
<a href="/assets/chcp/pdf/coveragePolicies/medical/mm_0540_ablation.pdf">Ablative Treatment for Malignant Breast Tumor - (0540)</a>
<a href="/assets/chcp/pdf/coveragePolicies/medical/mm_0070_allergy.pdf">Allergy Testing and Non-Pharmacologic Treatment - (0070)</a>
<a href="#anchor">Top of page</a>
<a href="/help.html">Contact us</a>
</body></html>
"""


class CignaParserTests(TestCase):
    def test_parses_pdf_links_only(self):
        entries = parse_cigna_medical_a_z(CIGNA_SAMPLE_HTML)
        self.assertEqual(len(entries), 3)

    def test_resolves_relative_urls_against_static_cigna_com(self):
        entries = parse_cigna_medical_a_z(CIGNA_SAMPLE_HTML)
        for e in entries:
            self.assertTrue(e.url.startswith("https://static.cigna.com/"))

    def test_extracts_payer_policy_id_from_title_suffix(self):
        entries = parse_cigna_medical_a_z(CIGNA_SAMPLE_HTML)
        ids = {e.payer_policy_id for e in entries}
        self.assertIn("CPG024", ids)
        self.assertIn("0540", ids)

    def test_strips_id_suffix_from_displayed_title(self):
        entries = parse_cigna_medical_a_z(CIGNA_SAMPLE_HTML)
        titles = [e.title for e in entries]
        self.assertIn("Acupuncture", titles)
        for t in titles:
            self.assertFalse(
                t.endswith(")"), f"Title still has trailing id suffix: {t}"
            )

    def test_dedupes_repeated_links(self):
        html = CIGNA_SAMPLE_HTML + CIGNA_SAMPLE_HTML
        self.assertEqual(len(parse_cigna_medical_a_z(html)), 3)

    def test_registered_under_static_cigna_com_host(self):
        self.assertIn("static.cigna.com", PARSERS_BY_HOST)
        self.assertIs(PARSERS_BY_HOST["static.cigna.com"], parse_cigna_medical_a_z)


class KeywordTermsTests(TestCase):
    def test_extracts_alphanumeric_tokens_of_length_4_plus(self):
        terms = _keyword_terms("Acupuncture treatment")
        self.assertIn("acupuncture", terms)
        self.assertIn("treatment", terms)

    def test_keeps_short_medical_abbreviations(self):
        terms = _keyword_terms("MRI of knee")
        self.assertIn("mri", terms)

    def test_drops_short_common_words(self):
        terms = _keyword_terms("the for and")
        self.assertEqual(terms, [])

    def test_dedupes_across_inputs(self):
        terms = _keyword_terms("acupuncture", "Acupuncture please")
        self.assertEqual(terms.count("acupuncture"), 1)

    def test_handles_none_inputs(self):
        self.assertEqual(_keyword_terms(None, None), [])

    def test_splits_on_punctuation_not_just_whitespace(self):
        # Regression: "MRI/PET-scan" should yield three tokens, not one.
        terms = _keyword_terms("MRI/PET-scan of upper-back")
        self.assertIn("mri", terms)
        self.assertIn("pet", terms)
        self.assertIn("scan", terms)
        self.assertIn("upper", terms)


class PolicyEntryQueryTests(TestCase):
    def setUp(self):
        self.cigna = InsuranceCompany.objects.create(
            name="Cigna",
            regex=r"cigna",
            medical_policy_url="https://static.cigna.com/medical_a-z.html",
            medical_policy_url_is_static_index=True,
            medical_policy_name="Coverage Policies",
            is_major_commercial_payer=True,
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna",
            regex=r"aetna",
            medical_policy_url="https://www.aetna.com/cpbs",
            medical_policy_url_is_static_index=False,
            medical_policy_name="Clinical Policy Bulletins",
            is_major_commercial_payer=True,
        )
        self.cigna_acupuncture = PayerPolicyEntry.objects.create(
            insurance_company=self.cigna,
            title="Acupuncture",
            url="https://static.cigna.com/acupuncture.pdf",
            payer_policy_id="CPG024",
        )
        self.cigna_mri = PayerPolicyEntry.objects.create(
            insurance_company=self.cigna,
            title="MRI of the Knee",
            url="https://static.cigna.com/mri-knee.pdf",
            payer_policy_id="0540",
        )
        # Aetna intentionally has no entries -- search-only portal.

    def test_returns_empty_when_no_keywords(self):
        self.assertEqual(get_relevant_policy_entries(None, None), [])
        self.assertEqual(get_relevant_policy_entries("", ""), [])

    def test_matches_procedure_keyword(self):
        out = get_relevant_policy_entries("Acupuncture", None)
        self.assertEqual([e.url for e in out], [self.cigna_acupuncture.url])

    def test_matches_short_abbreviation_keyword(self):
        out = get_relevant_policy_entries("MRI of knee", None)
        self.assertIn(self.cigna_mri, out)

    def test_filters_to_specific_company(self):
        out = get_relevant_policy_entries("Acupuncture", None, company_id=self.aetna.id)
        self.assertEqual(out, [])

    def test_excludes_company_for_comparative(self):
        out = get_relevant_policy_entries(
            "Acupuncture", None, exclude_company_id=self.cigna.id
        )
        self.assertEqual(out, [])


class PayerPolicyContextTests(TestCase):
    def setUp(self):
        self.cigna = InsuranceCompany.objects.create(
            name="Cigna",
            regex=r"cigna",
            medical_policy_url="https://static.cigna.com/medical_a-z.html",
            medical_policy_url_is_static_index=True,
            medical_policy_name="Coverage Policies",
            is_major_commercial_payer=True,
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna",
            regex=r"aetna",
            medical_policy_url="https://www.aetna.com/cpbs",
            medical_policy_url_is_static_index=False,
            medical_policy_name="Clinical Policy Bulletins",
            is_major_commercial_payer=True,
        )
        self.tiny = InsuranceCompany.objects.create(
            name="Tiny Co-op",
            regex=r"tiny",
        )
        PayerPolicyEntry.objects.create(
            insurance_company=self.cigna,
            title="Acupuncture",
            url="https://static.cigna.com/acupuncture.pdf",
            payer_policy_id="CPG024",
        )

    def test_format_company_block_returns_none_when_no_policy(self):
        self.assertIsNone(_format_company_block(self.tiny))

    def test_format_company_block_static_url_rendered_plainly(self):
        block = _format_company_block(self.cigna)
        assert block is not None
        self.assertIn("(https://static.cigna.com/medical_a-z.html)", block)
        self.assertNotIn("interactive search", block)

    def test_format_company_block_interactive_url_marked(self):
        block = _format_company_block(self.aetna)
        assert block is not None
        self.assertIn("interactive search", block)

    def test_own_policy_empty_when_company_none(self):
        self.assertEqual(get_payer_policy_context(None), "")

    def test_own_policy_surfaces_matched_entry_when_procedure_matches(self):
        ctx = get_payer_policy_context(self.cigna, procedure="Acupuncture")
        self.assertIn("Matched policies", ctx)
        self.assertIn("Acupuncture", ctx)
        self.assertIn("https://static.cigna.com/acupuncture.pdf", ctx)
        self.assertIn(WITH_ENTRIES_GUARD, ctx)

    def test_own_policy_falls_back_to_no_entries_guard_when_no_match(self):
        ctx = get_payer_policy_context(self.cigna, procedure="Surgery")
        self.assertNotIn("Matched policies", ctx)
        self.assertIn(NO_ENTRIES_GUARD, ctx)

    def test_comparative_includes_caveat(self):
        ctx = get_comparative_payer_policy_context()
        self.assertIn(COMPARATIVE_PAYER_CAVEAT, ctx)

    def test_comparative_excludes_specified_company(self):
        ctx = get_comparative_payer_policy_context(exclude_company_id=self.cigna.id)
        self.assertNotIn("**Cigna**", ctx)
        self.assertIn("**Aetna**", ctx)

    def test_comparative_surfaces_matched_entries(self):
        ctx = get_comparative_payer_policy_context(procedure="Acupuncture")
        self.assertIn("Specific matched policies", ctx)
        self.assertIn("https://static.cigna.com/acupuncture.pdf", ctx)
        self.assertIn(WITH_ENTRIES_GUARD, ctx)

    def test_comparative_uses_no_entries_guard_when_no_match(self):
        # Acupuncture matches Cigna; excluding Cigna leaves zero matches.
        ctx = get_comparative_payer_policy_context(
            procedure="Acupuncture",
            exclude_company_id=self.cigna.id,
        )
        self.assertNotIn("Specific matched policies", ctx)
        self.assertIn(NO_ENTRIES_GUARD, ctx)

    def test_comparative_drops_matched_entries_outside_listed_payers(self):
        # Add a third payer (Sentinel) with a matching policy entry. With
        # max_payers=1, only Aetna (alphabetic + major) is listed -- so
        # Sentinel's matched entry must NOT leak into the matched-entries
        # block, even though it would otherwise match.
        sentinel = InsuranceCompany.objects.create(
            name="Sentinel Out-Of-Scope",
            regex=r"sentinel",
            medical_policy_url="https://example.com/sentinel",
            medical_policy_url_is_static_index=True,
            medical_policy_name="Coverage Policies",
            is_major_commercial_payer=True,
        )
        PayerPolicyEntry.objects.create(
            insurance_company=sentinel,
            title="Acupuncture",
            url="https://example.com/sentinel-acupuncture.pdf",
        )
        ctx = get_comparative_payer_policy_context(
            procedure="Acupuncture", max_payers=1
        )
        self.assertIn("**Aetna**", ctx)
        self.assertNotIn("**Sentinel Out-Of-Scope**", ctx)
        self.assertNotIn("sentinel-acupuncture.pdf", ctx)

    def test_comparative_includes_payer_with_only_url(self):
        # Regression for the AND/OR queryset bug: a payer with only a URL
        # (or only a name) should still show up.
        InsuranceCompany.objects.create(
            name="UrlOnly Health",
            regex=r"urlonly",
            medical_policy_url="https://example.com/regional",
            medical_policy_name="",
        )
        InsuranceCompany.objects.create(
            name="NameOnly Health",
            regex=r"nameonly",
            medical_policy_url="",
            medical_policy_name="Medical Policies",
        )
        ctx = get_comparative_payer_policy_context(max_payers=10)
        self.assertIn("**UrlOnly Health**", ctx)
        self.assertIn("**NameOnly Health**", ctx)

    def test_comparative_prefers_major_commercial_payers(self):
        # Wipe setUp companies so the assertion isolates the major-vs-not
        # ordering from any alphabetical neighbours.
        InsuranceCompany.objects.all().delete()
        InsuranceCompany.objects.create(
            name="Aaardvark Regional BCBS",
            regex=r"aaardvark",
            medical_policy_url="https://example.com/regional",
            medical_policy_name="Medical Policies",
            is_major_commercial_payer=False,
        )
        InsuranceCompany.objects.create(
            name="Zenith National",
            regex=r"zenith",
            medical_policy_url="https://example.com/zenith",
            medical_policy_name="Coverage Policies",
            is_major_commercial_payer=True,
        )
        ctx = get_comparative_payer_policy_context(max_payers=1)
        self.assertIn("**Zenith National**", ctx)
        self.assertNotIn("**Aaardvark Regional BCBS**", ctx)

    def test_combined_includes_both_sections_when_company_known(self):
        ctx = get_combined_payer_policy_context(self.cigna)
        self.assertIn("Payer's Public Medical Policy", ctx)
        self.assertIn("Comparative Payer Medical Policies", ctx)
        self.assertIn(COMPARATIVE_PAYER_CAVEAT, ctx)
        comparative = ctx.split("Comparative Payer Medical Policies", 1)[1]
        self.assertNotIn("**Cigna**", comparative)

    def test_combined_can_skip_comparative_section(self):
        ctx = get_combined_payer_policy_context(self.cigna, include_comparative=False)
        self.assertIn("Cigna", ctx)
        self.assertNotIn("Comparative Payer Medical Policies", ctx)


class ResolveCompanyFromTextTests(TestCase):
    def setUp(self):
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna",
            alt_names="Aetna Inc\nCVS Health Aetna",
            regex=r"aetna",
        )

    def test_returns_none_for_blank(self):
        self.assertIsNone(resolve_company_from_text(None))
        self.assertIsNone(resolve_company_from_text(""))
        self.assertIsNone(resolve_company_from_text("   "))

    def test_exact_name_match_case_insensitive(self):
        self.assertEqual(resolve_company_from_text("aetna"), self.aetna)
        self.assertEqual(resolve_company_from_text("AETNA"), self.aetna)

    def test_alt_name_match(self):
        self.assertEqual(resolve_company_from_text("Aetna Inc"), self.aetna)
        self.assertEqual(resolve_company_from_text("CVS Health Aetna"), self.aetna)

    def test_no_match_returns_none(self):
        self.assertIsNone(resolve_company_from_text("Some Made-up Insurer"))


class FetcherIngestTests(TransactionTestCase):
    """End-to-end ingest test with mocked HTTP. Verifies that running the
    fetcher writes the parsed PayerPolicyEntry rows and that the helper
    surfaces them via keyword match.

    Uses TransactionTestCase because the fetcher runs DB writes inside an
    asyncio loop via sync_to_async; under SQLite, the regular TestCase's
    outer transaction would lock the connection from the inner sync call.
    """

    def setUp(self):
        self.cigna = InsuranceCompany.objects.create(
            name="Cigna",
            regex=r"cigna",
            medical_policy_url="https://static.cigna.com/medical_a-z.html",
            medical_policy_url_is_static_index=True,
            medical_policy_name="Coverage Policies",
            is_major_commercial_payer=True,
        )

    def test_ingest_writes_entries_and_helper_finds_them(self):
        async def run() -> int:
            fetcher = PayerPolicyFetcher()
            with patch.object(fetcher, "_get_text", return_value=CIGNA_SAMPLE_HTML):
                return await fetcher.ingest_company(self.cigna)

        count = asyncio.run(run())
        self.assertEqual(count, 3)
        self.assertEqual(
            PayerPolicyEntry.objects.filter(insurance_company=self.cigna).count(),
            3,
        )
        out = get_relevant_policy_entries("Acupuncture", None)
        self.assertTrue(out)
        self.assertIn("Acupuncture", out[0].title)
        self.assertTrue(out[0].url.startswith("https://static.cigna.com/"))

    def test_re_ingest_replaces_old_entries(self):
        # Pre-populate one stale entry that's no longer in the source.
        PayerPolicyEntry.objects.create(
            insurance_company=self.cigna,
            title="Stale Removed Policy",
            url="https://static.cigna.com/stale.pdf",
        )

        async def run() -> None:
            fetcher = PayerPolicyFetcher()
            with patch.object(fetcher, "_get_text", return_value=CIGNA_SAMPLE_HTML):
                await fetcher.ingest_company(self.cigna)

        asyncio.run(run())
        self.assertFalse(
            PayerPolicyEntry.objects.filter(
                insurance_company=self.cigna,
                title="Stale Removed Policy",
            ).exists()
        )

    def test_ingest_company_raises_when_no_parser_for_host(self):
        # An InsuranceCompany pointing at a host with no registered parser
        # must surface as a failed ingest, not a silent zero-entry success.
        unsupported = InsuranceCompany.objects.create(
            name="Unsupported Payer",
            regex=r"unsupported",
            medical_policy_url="https://example.invalid/policies.html",
            medical_policy_url_is_static_index=True,
            medical_policy_name="Coverage Policies",
        )

        async def run() -> None:
            fetcher = PayerPolicyFetcher()
            await fetcher.ingest_company(unsupported)

        with self.assertRaises(LookupError):
            asyncio.run(run())

    def test_ingest_all_counts_unsupported_host_as_failed(self):
        InsuranceCompany.objects.create(
            name="Unsupported Payer",
            regex=r"unsupported",
            medical_policy_url="https://example.invalid/policies.html",
            medical_policy_url_is_static_index=True,
            medical_policy_name="Coverage Policies",
        )

        async def run() -> dict:
            fetcher = PayerPolicyFetcher()
            with patch.object(fetcher, "_get_text", return_value=CIGNA_SAMPLE_HTML):
                return await fetcher.ingest_all()

        stats = asyncio.run(run())
        # Cigna ingests successfully; the unsupported host bumps `failed`.
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(stats["failed"], 1)


class MakeOpenPromptPayerPolicyTests(TestCase):
    """Test that make_open_prompt accepts and renders the payer-policy context."""

    def test_make_open_prompt_includes_payer_policy_context(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        sentinel = "## Comparative Payer Medical Policies\n(test block)"

        prompt = generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="MRI",
            diagnosis="Back pain",
            insurance_company="Aetna",
            payer_policy_context=sentinel,
        )

        assert prompt is not None
        self.assertIn(sentinel, prompt)
        self.assertIn("plan documents", prompt)

    def test_make_open_prompt_omits_payer_policy_when_blank(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        prompt = generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="MRI",
            diagnosis="Back pain",
            insurance_company="Aetna",
            payer_policy_context="",
        )
        assert prompt is not None
        self.assertNotIn("Comparative Payer Medical Policies", prompt)

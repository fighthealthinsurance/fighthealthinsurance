"""Unit tests for the structured PubMed search helpers.

Covers:
- ``pubmed_search`` pure helpers (query building, evidence classification,
  snippet formatting, bucket categorization).
- ``PubMedTools.fetch_mesh_and_pub_types`` and
  ``PubMedTools.find_related_pmids`` (E-utilities calls, mocked).
- ``PubMedTools.structured_search`` end-to-end (mocked PubMed + DB).

These tests don't hit the real PubMed API or the database where avoidable.
The end-to-end ``structured_search`` test is wrapped with the Django
``TransactionTestCase`` so the article materialization step can use the ORM.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase

from fighthealthinsurance.models import PubMedMiniArticle
from fighthealthinsurance.pubmed_search import (
    EvidenceStrength,
    MODERATE_PUB_TYPES,
    PUB_TYPE_PRESETS,
    STRONG_PUB_TYPES,
    WEAK_PUB_TYPES,
    build_structured_query,
    categorize_articles_by_strength,
    classify_evidence_strength,
    extract_publication_types,
    format_appeal_snippet,
    format_categorized_snippets,
    normalize_pub_type,
)
from fighthealthinsurance.pubmed_tools import PubMedTools

# ---------------------------------------------------------------------------
# build_structured_query
# ---------------------------------------------------------------------------


class TestBuildStructuredQuery:
    def test_empty_inputs_returns_empty_query(self):
        sq = build_structured_query()
        assert sq.query == ""
        assert sq.condition == ""
        assert sq.treatment == ""

    def test_condition_only(self):
        sq = build_structured_query(condition="rheumatoid arthritis")
        assert sq.query == '"rheumatoid arthritis"'
        assert sq.condition == '"rheumatoid arthritis"'
        assert sq.treatment == ""

    def test_treatment_only(self):
        sq = build_structured_query(treatment="infliximab")
        # Single-word terms aren't quoted.
        assert sq.query == "infliximab"

    def test_condition_and_treatment(self):
        sq = build_structured_query(
            condition="rheumatoid arthritis", treatment="physical therapy"
        )
        assert "rheumatoid arthritis" in sq.query
        assert "physical therapy" in sq.query
        assert " AND " in sq.query

    def test_pub_type_preset_guidelines(self):
        sq = build_structured_query(
            condition="asthma",
            pub_type_preset="guidelines_and_systematic_reviews",
        )
        assert "Guideline[pt]" in sq.query
        assert "Systematic Review[pt]" in sq.query
        assert "Meta-Analysis[pt]" in sq.query
        # Resolved filter list should match preset order.
        assert sq.pub_type_filters == list(
            PUB_TYPE_PRESETS["guidelines_and_systematic_reviews"]
        )

    def test_explicit_pub_type_filters(self):
        sq = build_structured_query(
            condition="asthma", pub_type_filters=["rct", "meta_analysis"]
        )
        assert "Randomized Controlled Trial[pt]" in sq.query
        assert "Meta-Analysis[pt]" in sq.query

    def test_unknown_pub_type_filter_is_dropped(self):
        sq = build_structured_query(
            condition="asthma", pub_type_filters=["bogus_filter", "rct"]
        )
        assert "Randomized Controlled Trial[pt]" in sq.query
        assert "bogus_filter" not in sq.query
        assert sq.pub_type_filters == ["rct"]

    def test_preset_and_explicit_dedup(self):
        sq = build_structured_query(
            condition="asthma",
            pub_type_preset="high_quality_evidence",
            pub_type_filters=["rct"],  # already in preset
        )
        # rct should appear only once.
        assert sq.pub_type_filters.count("rct") == 1

    def test_mesh_terms_use_mh_tag(self):
        sq = build_structured_query(
            condition="arthritis", mesh_terms=["Arthritis, Rheumatoid"]
        )
        assert '"Arthritis, Rheumatoid"[mh]' in sq.query
        assert sq.mesh_terms == ["Arthritis, Rheumatoid"]

    def test_since_year_creates_date_filter(self):
        sq = build_structured_query(condition="asthma", since_year="2020")
        assert '"2020"[dp]' in sq.query
        # Upper bound defaults to current year.
        assert f'"{datetime.now().year}"[dp]' in sq.query

    def test_since_and_until_year(self):
        sq = build_structured_query(
            condition="asthma", since_year="2018", until_year="2022"
        )
        assert '"2018"[dp]' in sq.query
        assert '"2022"[dp]' in sq.query

    def test_until_year_only(self):
        sq = build_structured_query(condition="asthma", until_year="2010")
        assert '"1900"[dp]' in sq.query
        assert '"2010"[dp]' in sq.query

    def test_extra_terms_appended(self):
        sq = build_structured_query(
            condition="asthma", extra_terms=["pediatric", "severe asthma"]
        )
        assert "pediatric" in sq.query
        assert '"severe asthma"' in sq.query

    def test_reserved_chars_are_stripped(self):
        sq = build_structured_query(condition="asthma[ti] (pediatric)")
        # Brackets and parentheses must be removed so the user can't inject
        # arbitrary PubMed search field tags.
        assert "[" not in sq.condition
        assert "]" not in sq.condition
        assert "(" not in sq.condition
        assert ")" not in sq.condition

    def test_combination_of_all_filters(self):
        sq = build_structured_query(
            condition="rheumatoid arthritis",
            treatment="infliximab",
            pub_type_preset="high_quality_evidence",
            mesh_terms=["Arthritis, Rheumatoid"],
            since_year="2020",
            extra_terms=["adult"],
        )
        # All ingredients should be present in the final query.
        assert "rheumatoid arthritis" in sq.query
        assert "infliximab" in sq.query
        assert "Meta-Analysis[pt]" in sq.query
        assert "[mh]" in sq.query
        assert '"2020"[dp]' in sq.query
        assert "adult" in sq.query


# ---------------------------------------------------------------------------
# Publication type extraction & classification
# ---------------------------------------------------------------------------


class TestPubTypeExtraction:
    def test_extract_from_publication_types_attr(self):
        article = MagicMock()
        article.publication_types = ["Journal Article", "Meta-Analysis"]
        # Wipe other attrs so MagicMock auto-attrs don't shadow.
        del article.pubtype
        del article.pub_types
        types = extract_publication_types(article)
        assert "Meta-Analysis" in types

    def test_extract_from_pubtype_dict(self):
        article = MagicMock(spec=["pubtype"])
        article.pubtype = {"D016428": "Journal Article", "D017418": "Meta-Analysis"}
        types = extract_publication_types(article)
        assert set(types) == {"Journal Article", "Meta-Analysis"}

    def test_extract_from_string_value(self):
        article = MagicMock(spec=["publication_types"])
        article.publication_types = "Review"
        assert extract_publication_types(article) == ["Review"]

    def test_extract_returns_empty_when_no_attrs(self):
        article = MagicMock(spec=[])
        assert extract_publication_types(article) == []

    def test_empty_first_attr_falls_through_to_fallback(self):
        # Regression: an empty list on the primary attr must not shadow a
        # populated fallback attr. metapub-like providers sometimes fill
        # `pubtype` but leave `publication_types` empty (or vice versa).
        article = MagicMock(spec=["publication_types", "pubtype"])
        article.publication_types = []
        article.pubtype = ["Meta-Analysis"]
        assert extract_publication_types(article) == ["Meta-Analysis"]

    def test_empty_string_first_attr_falls_through(self):
        article = MagicMock(spec=["publication_types", "pubtype"])
        article.publication_types = ""
        article.pubtype = ["Review"]
        assert extract_publication_types(article) == ["Review"]

    def test_empty_dict_first_attr_falls_through(self):
        article = MagicMock(spec=["publication_types", "pub_types"])
        article.publication_types = {}
        article.pub_types = ["Guideline"]
        assert extract_publication_types(article) == ["Guideline"]

    def test_normalize_pub_type(self):
        assert normalize_pub_type("Meta-Analysis") == "meta-analysis"
        assert normalize_pub_type(" Review  ") == "review"
        assert normalize_pub_type(None) == ""


class TestClassifyEvidenceStrength:
    @pytest.mark.parametrize("pub_type", sorted(STRONG_PUB_TYPES))
    def test_strong_types_classify_as_strong(self, pub_type):
        assert classify_evidence_strength([pub_type]) == EvidenceStrength.STRONG

    @pytest.mark.parametrize("pub_type", sorted(MODERATE_PUB_TYPES))
    def test_moderate_types_classify_as_moderate(self, pub_type):
        assert classify_evidence_strength([pub_type]) == EvidenceStrength.MODERATE

    @pytest.mark.parametrize("pub_type", sorted(WEAK_PUB_TYPES))
    def test_weak_types_classify_as_weak(self, pub_type):
        assert classify_evidence_strength([pub_type]) == EvidenceStrength.WEAK

    def test_strongest_tier_wins_when_multiple_present(self):
        # An article tagged Review (moderate) AND Meta-Analysis (strong)
        # must classify as STRONG.
        result = classify_evidence_strength(["Review", "Meta-Analysis"])
        assert result == EvidenceStrength.STRONG

    def test_unknown_types_classify_as_unknown(self):
        result = classify_evidence_strength(["Journal Article"])
        assert result == EvidenceStrength.UNKNOWN

    def test_empty_list_classifies_as_unknown(self):
        assert classify_evidence_strength([]) == EvidenceStrength.UNKNOWN

    def test_case_insensitive_match(self):
        # Authors of NCBI metadata use Title Case; our set uses lowercase.
        # Classification must work either way.
        assert classify_evidence_strength(["META-ANALYSIS"]) == EvidenceStrength.STRONG
        assert (
            classify_evidence_strength(["random ized controlled trial"])
            == EvidenceStrength.UNKNOWN
        )  # whitespace/typo doesn't accidentally match


# ---------------------------------------------------------------------------
# categorize_articles_by_strength
# ---------------------------------------------------------------------------


def _make_article(
    pmid,
    title="t",
    abstract="a",
    journal="J",
    year="2024",
    publication_types=None,
    basic_summary=None,
    article_url="",
):
    """Make a lightweight stand-in for a PubMedMiniArticle/-Summarized."""
    art = MagicMock(
        spec=[
            "pmid",
            "title",
            "abstract",
            "journal",
            "year",
            "publication_types",
            "basic_summary",
            "article_url",
        ]
    )
    art.pmid = pmid
    art.title = title
    art.abstract = abstract
    art.journal = journal
    art.year = year
    art.publication_types = publication_types
    art.basic_summary = basic_summary
    art.article_url = article_url
    return art


class TestCategorizeArticles:
    def test_uses_lookup_when_provided(self):
        # The lookup overrides whatever the article object reports.
        a = _make_article("1", publication_types=["Editorial"])  # would be weak
        buckets = categorize_articles_by_strength(
            [a], pub_types_lookup={"1": ["Meta-Analysis"]}
        )
        assert a in buckets[EvidenceStrength.STRONG]
        assert a not in buckets[EvidenceStrength.WEAK]

    def test_falls_back_to_article_attrs(self):
        a = _make_article("1", publication_types=["Meta-Analysis"])
        buckets = categorize_articles_by_strength([a])
        assert a in buckets[EvidenceStrength.STRONG]

    def test_all_tiers_always_present(self):
        buckets = categorize_articles_by_strength([])
        assert set(buckets.keys()) == set(EvidenceStrength)
        for tier in EvidenceStrength:
            assert buckets[tier] == []

    def test_mixed_set_distributes_correctly(self):
        strong = _make_article("1", publication_types=["Meta-Analysis"])
        moderate = _make_article("2", publication_types=["Review"])
        weak = _make_article("3", publication_types=["Editorial"])
        unknown = _make_article("4", publication_types=["Journal Article"])
        buckets = categorize_articles_by_strength([strong, moderate, weak, unknown])
        assert buckets[EvidenceStrength.STRONG] == [strong]
        assert buckets[EvidenceStrength.MODERATE] == [moderate]
        assert buckets[EvidenceStrength.WEAK] == [weak]
        assert buckets[EvidenceStrength.UNKNOWN] == [unknown]

    def test_preserves_input_order_within_bucket(self):
        a1 = _make_article("1", publication_types=["Meta-Analysis"])
        a2 = _make_article("2", publication_types=["Systematic Review"])
        a3 = _make_article("3", publication_types=["Practice Guideline"])
        buckets = categorize_articles_by_strength([a1, a2, a3])
        assert buckets[EvidenceStrength.STRONG] == [a1, a2, a3]


# ---------------------------------------------------------------------------
# Appeal-friendly snippet formatting
# ---------------------------------------------------------------------------


class TestFormatAppealSnippet:
    def test_basic_snippet_includes_citation_and_excerpt(self):
        a = _make_article(
            "12345",
            title="Effective therapy for arthritis",
            journal="JAMA",
            year="2023",
            article_url="https://pubmed.ncbi.nlm.nih.gov/12345/",
            basic_summary="This trial showed strong improvement. Patients tolerated the drug well. Follow-up was 12 months.",
        )
        snippet = format_appeal_snippet(a)
        assert "Effective therapy for arthritis" in snippet
        assert "JAMA, 2023" in snippet
        assert "PMID: 12345" in snippet
        assert "https://pubmed.ncbi.nlm.nih.gov/12345/" in snippet
        assert "Excerpt:" in snippet
        # First three sentences should be present.
        assert "strong improvement" in snippet
        assert "tolerated the drug well" in snippet
        assert "12 months" in snippet

    def test_prefers_basic_summary_over_abstract(self):
        a = _make_article(
            "1",
            basic_summary="Summary sentence one.",
            abstract="Abstract sentence one. Abstract sentence two.",
        )
        snippet = format_appeal_snippet(a)
        assert "Summary sentence one" in snippet
        assert "Abstract sentence" not in snippet

    def test_falls_back_to_abstract_when_no_summary(self):
        a = _make_article("1", basic_summary=None, abstract="The abstract.")
        snippet = format_appeal_snippet(a)
        assert "The abstract" in snippet

    def test_truncates_long_excerpt(self):
        # Three long sentences so the first-3-sentence selection still
        # exceeds max_chars and triggers truncation.
        long_sentence = "x" * 300 + "."
        long_text = " ".join([long_sentence] * 3)
        a = _make_article("1", basic_summary=long_text)
        snippet = format_appeal_snippet(a, max_chars=100)
        assert "…" in snippet
        excerpt_line = snippet.split("Excerpt: ", 1)[1]
        assert len(excerpt_line) <= 101  # max_chars + ellipsis

    def test_handles_missing_metadata(self):
        a = _make_article("", title="", journal="", year="", basic_summary="")
        # Should not crash; returns empty or just the citation line.
        snippet = format_appeal_snippet(a)
        assert isinstance(snippet, str)


class TestFormatCategorizedSnippets:
    def test_omits_empty_sections_by_default(self):
        strong = _make_article(
            "1", title="Strong study", basic_summary="Found benefit."
        )
        buckets = {
            EvidenceStrength.STRONG: [strong],
            EvidenceStrength.MODERATE: [],
            EvidenceStrength.WEAK: [],
            EvidenceStrength.UNKNOWN: [],
        }
        out = format_categorized_snippets(buckets)
        assert "## Strong evidence" in out
        assert "## Moderate evidence" not in out
        assert "## Weak but relevant evidence" not in out

    def test_include_empty_renders_all_sections(self):
        out = format_categorized_snippets(
            {tier: [] for tier in EvidenceStrength}, include_empty=True
        )
        assert "## Strong evidence" in out
        assert "## Moderate evidence" in out
        assert "## Weak but relevant evidence" in out
        assert "## Other evidence" in out
        assert out.count("(no articles)") == 4

    def test_renders_articles_as_bulleted_list(self):
        s1 = _make_article("1", title="A", basic_summary="findA")
        s2 = _make_article("2", title="B", basic_summary="findB")
        buckets = {
            EvidenceStrength.STRONG: [s1, s2],
            EvidenceStrength.MODERATE: [],
            EvidenceStrength.WEAK: [],
            EvidenceStrength.UNKNOWN: [],
        }
        out = format_categorized_snippets(buckets)
        # Each article is bulleted.
        assert out.count("- ") >= 2


# ---------------------------------------------------------------------------
# PubMedTools.fetch_mesh_and_pub_types
# ---------------------------------------------------------------------------


_EFETCH_XML_FIXTURE = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>11111111</PMID>
      <Article>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D017418">Meta-Analysis</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading>
          <DescriptorName UI="D001172">Arthritis, Rheumatoid</DescriptorName>
        </MeshHeading>
        <MeshHeading>
          <DescriptorName UI="D026741">Antirheumatic Agents</DescriptorName>
        </MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>22222222</PMID>
      <Article>
        <PublicationTypeList>
          <PublicationType UI="D016454">Review</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


def _make_aiohttp_response(text="", status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_aiohttp_session(response):
    session = AsyncMock()
    session.get = MagicMock(return_value=response)
    return session


@pytest.mark.asyncio
async def test_fetch_mesh_and_pub_types_parses_xml():
    tools = PubMedTools()
    session = _make_aiohttp_session(_make_aiohttp_response(text=_EFETCH_XML_FIXTURE))
    result = await tools.fetch_mesh_and_pub_types(
        ["11111111", "22222222"], session=session, timeout=5.0
    )
    assert result["11111111"]["pub_types"] == ["Journal Article", "Meta-Analysis"]
    assert "Arthritis, Rheumatoid" in result["11111111"]["mesh"]
    assert "Antirheumatic Agents" in result["11111111"]["mesh"]
    assert result["22222222"]["pub_types"] == ["Review"]
    assert result["22222222"]["mesh"] == []


@pytest.mark.asyncio
async def test_fetch_mesh_and_pub_types_handles_empty_input():
    tools = PubMedTools()
    out = await tools.fetch_mesh_and_pub_types([])
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_mesh_and_pub_types_handles_http_error():
    tools = PubMedTools()
    session = _make_aiohttp_session(_make_aiohttp_response(status=500, text=""))
    out = await tools.fetch_mesh_and_pub_types(["1"], session=session, timeout=5.0)
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_mesh_and_pub_types_handles_malformed_xml():
    tools = PubMedTools()
    session = _make_aiohttp_session(_make_aiohttp_response(text="<not xml"))
    out = await tools.fetch_mesh_and_pub_types(["1"], session=session, timeout=5.0)
    assert out == {}


# ---------------------------------------------------------------------------
# PubMedTools.find_related_pmids
# ---------------------------------------------------------------------------


_ELINK_JSON_FIXTURE = {
    "linksets": [
        {
            "linksetdbs": [
                {
                    "linkname": "pubmed_pubmed",
                    "links": ["11111111", "33333333", "44444444", "55555555"],
                },
                {
                    "linkname": "pubmed_pubmed_citedin",
                    "links": ["99999999"],
                },
            ]
        }
    ]
}


@pytest.mark.asyncio
async def test_find_related_pmids_excludes_seed():
    tools = PubMedTools()
    session = _make_aiohttp_session(
        _make_aiohttp_response(json_data=_ELINK_JSON_FIXTURE)
    )
    related = await tools.find_related_pmids(
        "11111111", session=session, max_results=10
    )
    assert "11111111" not in related
    assert related == ["33333333", "44444444", "55555555"]


@pytest.mark.asyncio
async def test_find_related_pmids_respects_max_results():
    tools = PubMedTools()
    session = _make_aiohttp_session(
        _make_aiohttp_response(json_data=_ELINK_JSON_FIXTURE)
    )
    related = await tools.find_related_pmids("11111111", session=session, max_results=2)
    assert len(related) == 2


@pytest.mark.asyncio
async def test_find_related_pmids_only_uses_pubmed_pubmed_linkname():
    tools = PubMedTools()
    session = _make_aiohttp_session(
        _make_aiohttp_response(json_data=_ELINK_JSON_FIXTURE)
    )
    related = await tools.find_related_pmids(
        "11111111", session=session, max_results=10
    )
    # 99999999 is from `pubmed_pubmed_citedin`, must not be included.
    assert "99999999" not in related


@pytest.mark.asyncio
async def test_find_related_pmids_handles_empty_pmid():
    tools = PubMedTools()
    related = await tools.find_related_pmids("")
    assert related == []


@pytest.mark.asyncio
async def test_find_related_pmids_handles_http_error():
    tools = PubMedTools()
    session = _make_aiohttp_session(_make_aiohttp_response(status=503, json_data={}))
    related = await tools.find_related_pmids(
        "11111111", session=session, max_results=10
    )
    assert related == []


# ---------------------------------------------------------------------------
# PubMedTools.structured_search end-to-end (DB + mocked PubMed API)
# ---------------------------------------------------------------------------


class StructuredSearchE2ETest(TransactionTestCase):
    """End-to-end: structured_search should run a query, fetch articles,
    fetch publication types, and return categorized buckets."""

    def setUp(self):
        # Pre-seed mini articles so structured_search doesn't need to call
        # metapub's article_by_pmid to materialize them.
        for pmid, title in [
            ("11111111", "Meta-analysis of biologic therapy for RA"),
            ("22222222", "Review of TNF inhibitors"),
            ("33333333", "Editorial commentary"),
        ]:
            PubMedMiniArticle.objects.create(
                pmid=pmid,
                title=title,
                abstract=f"Abstract for {title}",
                article_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )

    def test_structured_search_categorizes_results(self):
        tools = PubMedTools()

        async def fake_fetch_mesh_and_pub_types(pmids, **kwargs):
            return {
                "11111111": {"pub_types": ["Meta-Analysis"], "mesh": []},
                "22222222": {"pub_types": ["Review"], "mesh": []},
                "33333333": {"pub_types": ["Editorial"], "mesh": []},
            }

        async def fake_find_ids(query, since=None, timeout=60.0):
            return ["11111111", "22222222", "33333333"]

        with patch.object(
            tools, "find_pubmed_article_ids_for_query", side_effect=fake_find_ids
        ), patch.object(
            tools,
            "fetch_mesh_and_pub_types",
            side_effect=fake_fetch_mesh_and_pub_types,
        ):

            async def run():
                return await tools.structured_search(
                    condition="rheumatoid arthritis",
                    treatment="infliximab",
                    pub_type_preset="high_quality_evidence",
                )

            buckets = async_to_sync(run)()

            strong_pmids = [a.pmid for a in buckets[EvidenceStrength.STRONG]]
            moderate_pmids = [a.pmid for a in buckets[EvidenceStrength.MODERATE]]
            weak_pmids = [a.pmid for a in buckets[EvidenceStrength.WEAK]]

            assert "11111111" in strong_pmids
            assert "22222222" in moderate_pmids
            assert "33333333" in weak_pmids

    def test_structured_search_empty_query_returns_empty_buckets(self):
        tools = PubMedTools()

        async def run():
            return await tools.structured_search()

        buckets = async_to_sync(run)()
        assert set(buckets.keys()) == set(EvidenceStrength)
        for tier in EvidenceStrength:
            assert buckets[tier] == []

    def test_structured_search_with_expand_with_related(self):
        tools = PubMedTools()

        async def fake_find_ids(query, since=None, timeout=60.0):
            return ["11111111"]

        async def fake_related(seed, session=None, timeout=15.0, max_results=10):
            assert seed == "11111111"
            return ["22222222"]

        async def fake_fetch_pub_types(pmids, **kwargs):
            return {
                "11111111": {"pub_types": ["Meta-Analysis"], "mesh": []},
                "22222222": {"pub_types": ["Review"], "mesh": []},
            }

        with patch.object(
            tools, "find_pubmed_article_ids_for_query", side_effect=fake_find_ids
        ), patch.object(
            tools, "find_related_pmids", side_effect=fake_related
        ), patch.object(
            tools,
            "fetch_mesh_and_pub_types",
            side_effect=fake_fetch_pub_types,
        ):

            async def run():
                return await tools.structured_search(
                    condition="rheumatoid arthritis",
                    expand_with_related=True,
                )

            buckets = async_to_sync(run)()

            all_pmids = [a.pmid for tier in EvidenceStrength for a in buckets[tier]]
            # Both the seed and the related PMID should appear.
            assert "11111111" in all_pmids
            assert "22222222" in all_pmids

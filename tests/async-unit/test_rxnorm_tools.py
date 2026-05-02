"""Unit tests for RxNorm/RxNav integration."""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.models import RxNormConcept
from fighthealthinsurance.rxnorm_tools import (
    NormalizedDrug,
    RxNormTools,
    _clean_query,
    expand_drug_query_terms,
    normalize_drug_name,
)

# ---- Sample RxNav-shaped JSON responses --------------------------------

# Exact-match response for "atorvastatin".
EXACT_ATORVASTATIN = {"idGroup": {"name": "atorvastatin", "rxnormId": ["83367"]}}
# Properties for atorvastatin (RxCUI 83367).
PROPS_ATORVASTATIN = {
    "properties": {"rxcui": "83367", "name": "atorvastatin", "tty": "IN"}
}
# Approximate-match response for misspelling "atrovastatn".
APPROX_ATORVASTATIN = {
    "approximateGroup": {
        "candidate": [
            {"rxcui": "83367", "score": "85", "rank": "1", "name": "atorvastatin"}
        ]
    }
}
# Related concepts for atorvastatin: ingredient + brand names.
RELATED_ATORVASTATIN = {
    "relatedGroup": {
        "conceptGroup": [
            {
                "tty": "IN",
                "conceptProperties": [{"rxcui": "83367", "name": "atorvastatin"}],
            },
            {
                "tty": "BN",
                "conceptProperties": [
                    {"rxcui": "153165", "name": "Lipitor"},
                    {"rxcui": "1000048", "name": "Atorvaliq"},
                ],
            },
        ]
    }
}

# Empty response — no idGroup match.
EXACT_MISS: Dict[str, Any] = {"idGroup": {"name": "asdfqwerty"}}
APPROX_MISS: Dict[str, Any] = {"approximateGroup": {"candidate": []}}


def _make_fetch_json_mock(routes: Dict[str, Optional[Dict[str, Any]]]):
    """Build a side-effect that returns canned JSON keyed by URL substring.

    Accepts ``(url, session)`` because ``RxNormTools._fetch_json`` now
    threads a shared session through every HTTP helper. The session arg
    is ignored — the mock just keys on the URL.
    """

    async def _fetch(url: str, session: Any = None) -> Optional[Dict[str, Any]]:
        for needle, payload in routes.items():
            if needle in url:
                return payload
        return None

    return _fetch


class TestCleanQuery:
    def test_lowercases_and_trims(self):
        assert _clean_query("  Lipitor  ") == "lipitor"

    def test_collapses_whitespace(self):
        assert _clean_query("metformin\t  500  mg") == "metformin 500 mg"

    def test_handles_empty(self):
        assert _clean_query("") == ""
        assert _clean_query(None) == ""  # type: ignore[arg-type]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRxNormToolsNormalize:
    async def test_exact_match_returns_canonical_name(self):
        tools = RxNormTools()
        routes = {
            "/rxcui.json?name=atorvastatin": EXACT_ATORVASTATIN,
            "/rxcui/83367/properties.json": PROPS_ATORVASTATIN,
            "/rxcui/83367/related.json": RELATED_ATORVASTATIN,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            result = await tools.normalize("Atorvastatin")

        assert result.matched
        assert result.canonical_name == "atorvastatin"
        assert result.rxcui == "83367"
        assert result.tty == "IN"
        assert "BN" in result.related
        brand_names = [c["name"] for c in result.related["BN"]]
        assert "Lipitor" in brand_names

    async def test_approximate_match_for_misspelling(self):
        tools = RxNormTools()
        routes = {
            # Exact match miss
            "/rxcui.json?name=atrovastatn": EXACT_MISS,
            "/approximateTerm.json": APPROX_ATORVASTATIN,
            "/rxcui/83367/properties.json": PROPS_ATORVASTATIN,
            "/rxcui/83367/related.json": RELATED_ATORVASTATIN,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            result = await tools.normalize("atrovastatn")

        assert result.matched
        assert result.canonical_name == "atorvastatin"
        assert result.rxcui == "83367"
        assert result.score == 85

    async def test_total_miss_is_cached_and_returns_unmatched(self):
        tools = RxNormTools()
        routes = {
            "/rxcui.json": EXACT_MISS,
            "/approximateTerm.json": APPROX_MISS,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ) as mock_fetch:
            first = await tools.normalize("zzzzzzzzz")
            assert not first.matched
            assert first.rxcui == ""
            calls_after_first = mock_fetch.call_count

            # Second call should be served from cache (no extra HTTP calls).
            second = await tools.normalize("zzzzzzzzz")
            assert not second.matched
            assert mock_fetch.call_count == calls_after_first

    async def test_cache_hit_avoids_http(self):
        tools = RxNormTools()
        # Pre-populate the cache.
        await RxNormConcept.objects.acreate(
            query="lipitor",
            rxcui="153165",
            canonical_name="Lipitor",
            tty="BN",
            score=100,
            related_json={
                "IN": [{"rxcui": "83367", "name": "atorvastatin"}],
            },
        )
        with patch.object(tools, "_fetch_json", new_callable=AsyncMock) as mock_fetch:
            result = await tools.normalize("Lipitor")

        assert mock_fetch.call_count == 0
        assert result.matched
        assert result.canonical_name == "Lipitor"
        assert result.related["IN"][0]["name"] == "atorvastatin"

    async def test_empty_input_returns_unmatched(self):
        tools = RxNormTools()
        result = await tools.normalize("")
        assert not result.matched
        assert result.rxcui == ""

    async def test_low_score_approximate_still_returned(self):
        # The normalize() call returns whatever RxNorm gave us; downstream
        # callers (e.g., PubMedTool._normalize_drug_terms) decide whether
        # to trust it. Confirm the score is preserved verbatim.
        tools = RxNormTools()
        low_approx = {
            "approximateGroup": {
                "candidate": [{"rxcui": "83367", "score": "55", "name": "atorvastatin"}]
            }
        }
        routes = {
            "/rxcui.json": EXACT_MISS,
            "/approximateTerm.json": low_approx,
            "/rxcui/83367/properties.json": PROPS_ATORVASTATIN,
            "/rxcui/83367/related.json": RELATED_ATORVASTATIN,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            result = await tools.normalize("atrovstatn")

        assert result.matched
        assert result.score == 55


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestExpandQueryTerms:
    async def test_expands_brand_to_canonical_plus_synonyms(self):
        tools = RxNormTools()
        routes = {
            "/rxcui.json?name=lipitor": {
                "idGroup": {"name": "Lipitor", "rxnormId": ["153165"]}
            },
            "/rxcui/153165/properties.json": {
                "properties": {"rxcui": "153165", "name": "Lipitor", "tty": "BN"}
            },
            "/rxcui/153165/related.json": {
                "relatedGroup": {
                    "conceptGroup": [
                        {
                            "tty": "IN",
                            "conceptProperties": [
                                {"rxcui": "83367", "name": "atorvastatin"}
                            ],
                        },
                        {
                            "tty": "BN",
                            "conceptProperties": [
                                {"rxcui": "153165", "name": "Lipitor"},
                                {"rxcui": "1000048", "name": "Atorvaliq"},
                            ],
                        },
                    ]
                }
            },
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            terms = await tools.expand_query_terms("Lipitor")

        # Canonical name first, then ingredients, then brand names. We
        # also de-duplicate (Lipitor would otherwise appear twice).
        assert terms[0] == "Lipitor"
        assert "atorvastatin" in terms
        assert "Atorvaliq" in terms
        # No duplicates.
        assert len(terms) == len(set(t.lower() for t in terms))

    async def test_falls_back_to_user_input_on_miss(self):
        tools = RxNormTools()
        routes = {
            "/rxcui.json": EXACT_MISS,
            "/approximateTerm.json": APPROX_MISS,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            terms = await tools.expand_query_terms("xyz123 tablet")

        assert terms == ["xyz123 tablet"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestGetBrandsAndGenerics:
    async def test_returns_structured_view(self):
        tools = RxNormTools()
        routes = {
            "/rxcui.json?name=atorvastatin": EXACT_ATORVASTATIN,
            "/rxcui/83367/properties.json": PROPS_ATORVASTATIN,
            "/rxcui/83367/related.json": RELATED_ATORVASTATIN,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            info = await tools.get_brands_and_generics("atorvastatin")

        assert info["matched"] is True
        assert info["canonical_name"] == "atorvastatin"
        assert info["ingredients"] == ["atorvastatin"]
        assert "Lipitor" in info["brand_names"]
        # Exact matches surface a score of 100 so callers can distinguish
        # them from approximate hits.
        assert info["score"] == 100

    async def test_unmatched_returns_empty_lists(self):
        tools = RxNormTools()
        routes = {
            "/rxcui.json": EXACT_MISS,
            "/approximateTerm.json": APPROX_MISS,
        }
        with patch.object(
            tools, "_fetch_json", side_effect=_make_fetch_json_mock(routes)
        ):
            info = await tools.get_brands_and_generics("nonexistentdrug")

        assert info["matched"] is False
        assert info["ingredients"] == []
        assert info["brand_names"] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestModuleHelpers:
    async def test_normalize_drug_name_uses_default_singleton(self):
        with patch(
            "fighthealthinsurance.rxnorm_tools.RxNormTools.normalize",
            new_callable=AsyncMock,
        ) as mock_normalize:
            mock_normalize.return_value = NormalizedDrug(
                query="lipitor",
                rxcui="153165",
                canonical_name="Lipitor",
                tty="BN",
            )
            result = await normalize_drug_name("Lipitor")

        assert result.matched
        assert result.canonical_name == "Lipitor"

    async def test_expand_drug_query_terms_caps_at_max_terms(self):
        with patch(
            "fighthealthinsurance.rxnorm_tools.RxNormTools.expand_query_terms",
            new_callable=AsyncMock,
        ) as mock_expand:
            mock_expand.return_value = ["a", "b", "c"]
            terms = await expand_drug_query_terms("anything", max_terms=3)

        assert terms == ["a", "b", "c"]
        mock_expand.assert_awaited_once()
        # max_terms is forwarded to the underlying tool.
        kwargs = mock_expand.await_args.kwargs
        assert kwargs.get("max_terms") == 3


class TestPubMedSingleDrugHeuristic:
    """Cover PubMedTool._looks_like_single_drug_query / _normalize_drug_terms."""

    def test_looks_like_drug_for_short_unitary_query(self):
        from fighthealthinsurance.chat.tools.pubmed_tool import PubMedTool

        assert PubMedTool._looks_like_single_drug_query("Lipitor")
        assert PubMedTool._looks_like_single_drug_query("metformin 500mg")
        assert PubMedTool._looks_like_single_drug_query("co-trimoxazole")

    def test_rejects_multi_concept_query(self):
        from fighthealthinsurance.chat.tools.pubmed_tool import PubMedTool

        # Multi-concept queries that would lose information if collapsed
        # to a canonical drug name.
        assert not PubMedTool._looks_like_single_drug_query(
            "metformin and kidney disease"
        )
        assert not PubMedTool._looks_like_single_drug_query("Lipitor vs simvastatin")
        assert not PubMedTool._looks_like_single_drug_query("metformin, atorvastatin")
        # Three-or-more-token query — even without a connector, refuse to
        # rewrite, since "metformin kidney disease" looks lexically like a
        # drug+dose pair but means drug+condition.
        assert not PubMedTool._looks_like_single_drug_query("metformin kidney disease")

    def test_rejects_empty(self):
        from fighthealthinsurance.chat.tools.pubmed_tool import PubMedTool

        assert not PubMedTool._looks_like_single_drug_query("")
        assert not PubMedTool._looks_like_single_drug_query("   ")

    @pytest.mark.asyncio
    async def test_normalize_drug_terms_skips_multi_concept(self):
        from fighthealthinsurance.chat.tools.pubmed_tool import PubMedTool

        send_status = AsyncMock()
        rxnorm = RxNormTools()
        # If the heuristic were bypassed and normalize() ran, we'd see a call.
        with patch.object(
            rxnorm, "normalize", new_callable=AsyncMock
        ) as mock_normalize:
            tool = PubMedTool(send_status, rxnorm_tools=rxnorm)
            result = await tool._normalize_drug_terms("metformin kidney disease")

        assert result == "metformin kidney disease"
        mock_normalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normalize_drug_terms_rewrites_single_drug(self):
        from fighthealthinsurance.chat.tools.pubmed_tool import PubMedTool

        send_status = AsyncMock()
        rxnorm = RxNormTools()
        with patch.object(
            rxnorm, "normalize", new_callable=AsyncMock
        ) as mock_normalize:
            mock_normalize.return_value = NormalizedDrug(
                query="lipitor",
                rxcui="153165",
                canonical_name="atorvastatin",
                tty="IN",
                score=100,
            )
            tool = PubMedTool(send_status, rxnorm_tools=rxnorm)
            result = await tool._normalize_drug_terms("Lipitor")

        assert result == "atorvastatin"
        mock_normalize.assert_awaited_once_with("Lipitor")


class TestRxNormLookupToolFormatting:
    """Cover RxNormLookupTool._format_context (score gating, _merge)."""

    def test_low_score_treated_as_miss(self):
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        info = {
            "matched": True,
            "rxcui": "83367",
            "canonical_name": "atorvastatin",
            "tty": "IN",
            "score": 50,
            "ingredients": ["atorvastatin"],
            "brand_names": ["Lipitor"],
        }
        out = RxNormLookupTool._format_context("atrovstatn", info)
        # Below threshold — should not include the canonical name; should
        # advise falling back to the user's spelling.
        assert "atorvastatin" not in out
        assert "no canonical RxNorm record" in out

    def test_exact_match_includes_canonical(self):
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        info = {
            "matched": True,
            "rxcui": "83367",
            "canonical_name": "atorvastatin",
            "tty": "IN",
            "score": 100,
            "ingredients": ["atorvastatin"],
            "brand_names": ["Lipitor"],
        }
        out = RxNormLookupTool._format_context("Atorvastatin", info)
        assert "canonical name: atorvastatin" in out
        assert "Lipitor" in out

    def test_merge_inserts_separator(self):
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        merged = RxNormLookupTool._merge("first sentence.", "second sentence.")
        assert merged == "first sentence.\n\nsecond sentence."

    def test_merge_handles_empties(self):
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        assert RxNormLookupTool._merge("", "") == ""
        assert RxNormLookupTool._merge(None, None) == ""
        assert RxNormLookupTool._merge("only", "") == "only"
        assert RxNormLookupTool._merge("", "only") == "only"


class TestRxNormLookupToolHandle:
    """End-to-end coverage of RxNormLookupTool.handle()/execute().

    Mocks the underlying ``RxNormTools`` so we don't hit the network.
    Specifically guards against regressions in: wrapper cleanup
    (``[...]`` / ``**...**``), score-based gating, and self-recursion
    safety on the resulting context.
    """

    @pytest.mark.asyncio
    async def test_handle_strips_bracket_wrapper(self):
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        send_status = AsyncMock()
        rxnorm = RxNormTools()
        with patch.object(
            rxnorm, "get_brands_and_generics", new_callable=AsyncMock
        ) as mock_info:
            mock_info.return_value = {
                "matched": True,
                "rxcui": "153165",
                "canonical_name": "atorvastatin",
                "tty": "IN",
                "score": 100,
                "ingredients": ["atorvastatin"],
                "brand_names": ["Lipitor"],
            }
            tool = RxNormLookupTool(send_status, rxnorm_tools=rxnorm)
            response_text = "Some response. [rxnorm_lookup: Lipitor] Trailing text."
            cleaned, ctx, handled = await tool.handle(response_text, "")

        assert handled
        # Trailing `]` from the tool call should be consumed entirely;
        # the surrounding response text should remain readable.
        assert "[rxnorm_lookup" not in cleaned
        assert "]" not in cleaned.split(".")[1]  # nothing dangling on second sentence
        assert "atorvastatin" in ctx
        mock_info.assert_awaited_once_with("Lipitor")

    @pytest.mark.asyncio
    async def test_handle_strips_asterisk_wrapper(self):
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        send_status = AsyncMock()
        rxnorm = RxNormTools()
        with patch.object(
            rxnorm, "get_brands_and_generics", new_callable=AsyncMock
        ) as mock_info:
            mock_info.return_value = {
                "matched": True,
                "rxcui": "6809",
                "canonical_name": "metformin",
                "tty": "IN",
                "score": 100,
                "ingredients": ["metformin"],
                "brand_names": ["Glucophage"],
            }
            tool = RxNormLookupTool(send_status, rxnorm_tools=rxnorm)
            cleaned, ctx, handled = await tool.handle(
                "**rxnorm_lookup: glucophage** continuing.", ""
            )

        assert handled
        # Both leading and trailing ** should be consumed.
        assert "**rxnorm_lookup" not in cleaned
        assert "**" not in cleaned.split("continuing")[0]
        assert "metformin" in ctx

    @pytest.mark.asyncio
    async def test_handle_does_not_match_prose(self):
        """Plain prose without an explicit tool call should be ignored."""
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        send_status = AsyncMock()
        rxnorm = RxNormTools()
        with patch.object(
            rxnorm, "get_brands_and_generics", new_callable=AsyncMock
        ) as mock_info:
            tool = RxNormLookupTool(send_status, rxnorm_tools=rxnorm)
            cleaned, ctx, handled = await tool.handle(
                "I might do an RxNorm lookup for the medication.", ""
            )

        assert not handled
        assert "RxNorm lookup for the medication" in cleaned
        mock_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emitted_context_is_not_self_triggering(self):
        """The formatted context must not contain a phrase that re-triggers
        the tool when the LLM echoes it back."""
        from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool

        info = {
            "matched": True,
            "rxcui": "153165",
            "canonical_name": "atorvastatin",
            "tty": "IN",
            "score": 100,
            "ingredients": ["atorvastatin"],
            "brand_names": ["Lipitor"],
        }
        formatted = RxNormLookupTool._format_context("Lipitor", info)

        # Run the formatted output back through the tool's pattern; if a
        # match is found, the tool would recurse on itself.
        send_status = AsyncMock()
        rxnorm = RxNormTools()
        tool = RxNormLookupTool(send_status, rxnorm_tools=rxnorm)
        assert tool.detect(formatted) is None


class TestPubMedToolsDrugExpansion:
    """Cover PubMedTools._looks_like_single_drug_term and
    ._expand_drug_term_for_search — used in find_pubmed_articles_for_denial
    and the extralink prefetch actor."""

    def test_looks_like_single_drug_short(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        assert PubMedTools._looks_like_single_drug_term("Lipitor")
        assert PubMedTools._looks_like_single_drug_term("metformin 500mg")

    def test_rejects_multi_concept(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        # Connector words and 3+ tokens are out.
        assert not PubMedTools._looks_like_single_drug_term("Lipitor and atorvastatin")
        assert not PubMedTools._looks_like_single_drug_term(
            "physical therapy arthritis"
        )
        assert not PubMedTools._looks_like_single_drug_term("")

    @pytest.mark.asyncio
    async def test_expand_drug_term_returns_canonical_and_synonyms(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        rxnorm = RxNormTools()
        tools = PubMedTools(rxnorm_tools=rxnorm)
        with patch.object(
            rxnorm, "normalize", new_callable=AsyncMock
        ) as mock_normalize:
            mock_normalize.return_value = NormalizedDrug(
                query="lipitor",
                rxcui="153165",
                canonical_name="atorvastatin",
                tty="BN",
                score=100,
                related={
                    "IN": [{"rxcui": "83367", "name": "atorvastatin"}],
                    "BN": [
                        {"rxcui": "153165", "name": "Lipitor"},
                        {"rxcui": "1000048", "name": "Atorvaliq"},
                    ],
                },
            )
            terms = await tools._expand_drug_term_for_search("Lipitor")

        # Canonical first, then ingredients, then brand names. Capped at
        # _MAX_DRUG_EXPANSIONS (2). The original input is excluded.
        assert "Lipitor" not in terms
        assert "atorvastatin" in terms
        assert len(terms) <= PubMedTools._MAX_DRUG_EXPANSIONS

    @pytest.mark.asyncio
    async def test_expand_skips_multi_concept_query(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        rxnorm = RxNormTools()
        tools = PubMedTools(rxnorm_tools=rxnorm)
        with patch.object(
            rxnorm, "normalize", new_callable=AsyncMock
        ) as mock_normalize:
            terms = await tools._expand_drug_term_for_search(
                "physical therapy arthritis"
            )

        assert terms == []
        mock_normalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expand_skips_low_score_match(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        rxnorm = RxNormTools()
        tools = PubMedTools(rxnorm_tools=rxnorm)
        with patch.object(
            rxnorm, "normalize", new_callable=AsyncMock
        ) as mock_normalize:
            mock_normalize.return_value = NormalizedDrug(
                query="atrovstatn",
                rxcui="83367",
                canonical_name="atorvastatin",
                tty="IN",
                score=55,
            )
            terms = await tools._expand_drug_term_for_search("atrovstatn")

        # Below the 75 confidence threshold — leave it alone.
        assert terms == []

    @pytest.mark.asyncio
    async def test_expand_no_match(self):
        from fighthealthinsurance.pubmed_tools import PubMedTools

        rxnorm = RxNormTools()
        tools = PubMedTools(rxnorm_tools=rxnorm)
        with patch.object(
            rxnorm, "normalize", new_callable=AsyncMock
        ) as mock_normalize:
            mock_normalize.return_value = NormalizedDrug(
                query="nonexistent",
                rxcui="",
                canonical_name="",
            )
            terms = await tools._expand_drug_term_for_search("nonexistent")

        assert terms == []


class TestPriorAuthRxNormContext:
    """Cover the RxNorm hint that gets added to the prior-auth prompt."""

    def test_drug_match_adds_canonical_hint(self):
        from fighthealthinsurance.generate_prior_auth import _build_rxnorm_context

        normalized = NormalizedDrug(
            query="lipitor",
            rxcui="153165",
            canonical_name="atorvastatin",
            tty="BN",
            score=100,
            related={
                "IN": [{"rxcui": "83367", "name": "atorvastatin"}],
                "BN": [{"rxcui": "153165", "name": "Lipitor"}],
            },
        )
        hint = _build_rxnorm_context("Lipitor", normalized)

        assert "atorvastatin" in hint
        # The prompt should explicitly tell the LLM to keep the user's
        # wording in the rendered letter.
        assert "use" in hint.lower()
        assert "Lipitor" in hint

    def test_no_match_returns_empty_hint(self):
        from fighthealthinsurance.generate_prior_auth import _build_rxnorm_context

        normalized = NormalizedDrug(
            query="mri",
            rxcui="",
            canonical_name="",
        )
        assert _build_rxnorm_context("MRI", normalized) == ""

    def test_low_score_returns_empty_hint(self):
        from fighthealthinsurance.generate_prior_auth import _build_rxnorm_context

        normalized = NormalizedDrug(
            query="atrovstatn",
            rxcui="83367",
            canonical_name="atorvastatin",
            tty="IN",
            score=55,
        )
        assert _build_rxnorm_context("atrovstatn", normalized) == ""

    def test_already_canonical_returns_empty_hint(self):
        from fighthealthinsurance.generate_prior_auth import _build_rxnorm_context

        # Treatment is already the canonical name — no rewriting needed.
        normalized = NormalizedDrug(
            query="atorvastatin",
            rxcui="83367",
            canonical_name="atorvastatin",
            tty="IN",
            score=100,
        )
        assert _build_rxnorm_context("atorvastatin", normalized) == ""

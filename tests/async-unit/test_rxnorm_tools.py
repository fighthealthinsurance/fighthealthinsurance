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
    """Build a side-effect that returns canned JSON keyed by URL substring."""

    async def _fetch(url: str) -> Optional[Dict[str, Any]]:
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

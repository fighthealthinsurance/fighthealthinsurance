"""Unit tests for the NICE (UK) syndication API client."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance.nice_tools import (
    INTERNATIONAL_GUIDANCE_CAVEAT,
    NICETools,
    PER_QUERY_LIMIT,
)


def _make_mock_response(
    status: int = 200,
    json_data=None,
    content_type: str = "application/json",
):
    resp = AsyncMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.json = AsyncMock(return_value=json_data or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_session(response):
    session = AsyncMock()
    session.get = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestNICEToolsHelpers:
    """Tests for the static / pure helpers on NICETools."""

    def test_build_query_combines_procedure_and_diagnosis(self):
        assert (
            NICETools._build_query("knee replacement", "osteoarthritis")
            == "knee replacement osteoarthritis"
        )

    def test_build_query_strips_blanks(self):
        assert NICETools._build_query("", "  diabetes  ") == "diabetes"
        assert NICETools._build_query("   ", "") == ""

    @pytest.mark.parametrize(
        "data,expected_count",
        [
            ({"documents": [{"id": "NG54", "title": "T"}]}, 1),
            ({"items": [{"id": "TA608", "title": "T"}]}, 1),
            (
                {"results": {"items": [{"id": "CG181", "title": "T"}]}},
                1,
            ),
            ({}, 0),
            ({"documents": "not a list"}, 0),
        ],
    )
    def test_extract_items_handles_response_shapes(self, data, expected_count):
        items = NICETools._extract_items(data)
        assert len(items) == expected_count

    def test_normalize_item_handles_lowercase_keys(self):
        normalized = NICETools._normalize_item(
            {
                "id": "NG54",
                "title": "Cancer follow-up",
                "url": "https://nice.org.uk/ng54",
                "type": "NICE guideline",
                "summary": "Recommendations for cancer survivors.",
            }
        )
        assert normalized is not None
        assert normalized["guidance_id"] == "NG54"
        assert normalized["title"] == "Cancer follow-up"
        assert normalized["url"] == "https://nice.org.uk/ng54"
        assert normalized["guidance_type"] == "NICE guideline"
        assert normalized["summary"] == "Recommendations for cancer survivors."

    def test_normalize_item_handles_capitalized_keys(self):
        normalized = NICETools._normalize_item(
            {
                "Reference": "TA608",
                "Title": "Drug X for Y",
                "Url": "https://nice.org.uk/ta608",
                "Type": "Technology appraisal",
                "Description": "Drug X is recommended.",
            }
        )
        assert normalized is not None
        assert normalized["guidance_id"] == "TA608"
        assert normalized["title"] == "Drug X for Y"

    def test_normalize_item_returns_none_when_required_fields_missing(self):
        assert NICETools._normalize_item({"title": "no id"}) is None
        assert NICETools._normalize_item({"id": "NG1"}) is None
        assert NICETools._normalize_item({}) is None

    def test_format_guidance_short_includes_caveat_friendly_fields(self):
        guidance = MagicMock()
        guidance.guidance_id = "NG54"
        guidance.guidance_type = "NICE guideline"
        guidance.title = "Cancer follow-up"
        guidance.url = "https://nice.org.uk/ng54"
        guidance.summary = "Short summary."
        formatted = NICETools.format_guidance_short(guidance)
        assert "NICE NG54" in formatted
        assert "Type: NICE guideline" in formatted
        assert "Title: Cancer follow-up" in formatted
        assert "URL: https://nice.org.uk/ng54" in formatted
        assert "Summary: Short summary." in formatted

    def test_format_guidance_short_truncates_long_summary(self):
        guidance = MagicMock()
        guidance.guidance_id = "NG1"
        guidance.guidance_type = ""
        guidance.title = "T"
        guidance.url = ""
        guidance.summary = "x" * 600
        formatted = NICETools.format_guidance_short(guidance)
        assert "..." in formatted
        # 400-char window plus "..." is the cap.
        assert len(formatted) < 600


class TestNICEToolsAuth:
    def test_auth_headers_include_api_key_when_set(self):
        tools = NICETools(api_key="secret")
        headers = tools._auth_headers()
        assert headers["Api-Key"] == "secret"
        assert headers["Ocp-Apim-Subscription-Key"] == "secret"

    def test_auth_headers_omit_api_key_when_unset(self):
        tools = NICETools(api_key="")
        headers = tools._auth_headers()
        assert "Api-Key" not in headers
        assert "Ocp-Apim-Subscription-Key" not in headers


@pytest.mark.asyncio
class TestNICESearchGuidance:
    async def test_returns_empty_for_empty_query(self):
        tools = NICETools(api_key="x")
        assert await tools.search_guidance("") == []
        assert await tools.search_guidance("   ") == []

    async def test_skips_api_call_without_key(self):
        tools = NICETools(api_key="")
        # Cache is empty in tests (no DB) — patch the lookup so we don't touch the ORM.
        with patch(
            "fighthealthinsurance.nice_tools.NICEQueryData.objects"
        ) as mock_objects:
            qs = MagicMock()
            qs.aexists = AsyncMock(return_value=False)
            qs.order_by = MagicMock(return_value=qs)
            mock_objects.filter = MagicMock(return_value=qs)
            with patch("aiohttp.ClientSession") as mock_session_cls:
                result = await tools.search_guidance("knee replacement")
                assert result == []
                mock_session_cls.assert_not_called()

    async def test_returns_normalized_items_from_api(self):
        tools = NICETools(api_key="key")

        api_response = {
            "documents": [
                {
                    "id": "NG54",
                    "title": "Cancer follow-up",
                    "url": "https://nice.org.uk/ng54",
                    "type": "NICE guideline",
                    "summary": "Summary text.",
                },
                {
                    "id": "TA608",
                    "title": "Drug X for Y",
                    "url": "https://nice.org.uk/ta608",
                    "type": "Technology appraisal",
                    "summary": "Drug X is recommended.",
                },
            ]
        }

        # Patch caching: nothing in cache, capture writes.
        with patch(
            "fighthealthinsurance.nice_tools.NICEQueryData.objects"
        ) as mock_objects:
            qs = MagicMock()
            qs.aexists = AsyncMock(return_value=False)
            qs.order_by = MagicMock(return_value=qs)
            mock_objects.filter = MagicMock(return_value=qs)
            mock_objects.acreate = AsyncMock()

            response = _make_mock_response(json_data=api_response)
            session = _make_mock_session(response)
            with patch("aiohttp.ClientSession", return_value=session):
                items = await tools.search_guidance("knee replacement")

            assert len(items) == 2
            assert items[0]["guidance_id"] == "NG54"
            assert items[1]["guidance_id"] == "TA608"
            # Cached the result.
            mock_objects.acreate.assert_awaited_once()
            kwargs = mock_objects.acreate.call_args.kwargs
            assert kwargs["query"] == "knee replacement"
            cached_items = json.loads(kwargs["results"])
            assert len(cached_items) == 2

    async def test_returns_cached_results_when_available(self):
        tools = NICETools(api_key="key")
        cached_items = [
            {
                "guidance_id": "NG54",
                "title": "Cached title",
                "url": "https://nice.org.uk/ng54",
                "guidance_type": "NICE guideline",
                "summary": "Cached summary.",
            }
        ]
        cached_row = MagicMock()
        cached_row.results = json.dumps(cached_items)

        with patch(
            "fighthealthinsurance.nice_tools.NICEQueryData.objects"
        ) as mock_objects:
            qs = MagicMock()
            qs.aexists = AsyncMock(return_value=True)
            qs.afirst = AsyncMock(return_value=cached_row)
            qs.order_by = MagicMock(return_value=qs)
            mock_objects.filter = MagicMock(return_value=qs)
            with patch("aiohttp.ClientSession") as mock_session_cls:
                items = await tools.search_guidance("knee replacement")
                # Cache hit: we shouldn't open an HTTP session.
                mock_session_cls.assert_not_called()

        assert items == cached_items

    async def test_truncates_to_per_query_limit(self):
        tools = NICETools(api_key="key")
        big_response = {
            "documents": [
                {"id": f"NG{i}", "title": f"T{i}", "url": "", "type": "", "summary": ""}
                for i in range(PER_QUERY_LIMIT + 5)
            ]
        }
        with patch(
            "fighthealthinsurance.nice_tools.NICEQueryData.objects"
        ) as mock_objects:
            qs = MagicMock()
            qs.aexists = AsyncMock(return_value=False)
            qs.order_by = MagicMock(return_value=qs)
            mock_objects.filter = MagicMock(return_value=qs)
            mock_objects.acreate = AsyncMock()

            response = _make_mock_response(json_data=big_response)
            session = _make_mock_session(response)
            with patch("aiohttp.ClientSession", return_value=session):
                items = await tools.search_guidance("query")
            assert len(items) == PER_QUERY_LIMIT


@pytest.mark.asyncio
class TestNICEContextFormatting:
    async def test_find_context_for_denial_includes_caveat(self):
        tools = NICETools(api_key="key")

        guidance = MagicMock()
        guidance.guidance_id = "NG54"
        guidance.guidance_type = "NICE guideline"
        guidance.title = "Cancer follow-up"
        guidance.url = "https://nice.org.uk/ng54"
        guidance.summary = "Short summary."

        denial = MagicMock()
        denial.denial_id = 1
        denial.nice_context = ""
        denial.procedure = "knee replacement"
        denial.diagnosis = "osteoarthritis"

        with patch.object(
            tools,
            "find_guidance_for_denial",
            new_callable=AsyncMock,
            return_value=[guidance],
        ):
            context = await tools._find_context_for_denial(denial)

        assert INTERNATIONAL_GUIDANCE_CAVEAT in context
        assert "NICE NG54" in context

    async def test_find_context_returns_empty_when_no_guidance(self):
        tools = NICETools(api_key="key")
        denial = MagicMock()
        denial.denial_id = 1
        denial.nice_context = ""
        denial.procedure = "knee replacement"
        denial.diagnosis = "osteoarthritis"
        with patch.object(
            tools,
            "find_guidance_for_denial",
            new_callable=AsyncMock,
            return_value=[],
        ):
            assert await tools._find_context_for_denial(denial) == ""

    async def test_find_context_uses_existing_persisted_context(self):
        tools = NICETools(api_key="key")
        denial = MagicMock()
        denial.denial_id = 1
        denial.nice_context = "previously computed context"
        denial.procedure = "x"
        denial.diagnosis = "y"
        # Should not call find_guidance_for_denial when nice_context is already set.
        with patch.object(
            tools, "find_guidance_for_denial", new_callable=AsyncMock
        ) as mock_find:
            result = await tools._find_context_for_denial(denial)
            mock_find.assert_not_called()
        assert result == "previously computed context"

    async def test_find_context_short_circuits_without_api_key(self):
        """Without a key we shouldn't hit the database or the network."""
        tools = NICETools(api_key="")
        denial = MagicMock()
        denial.denial_id = 1
        denial.nice_context = ""
        denial.procedure = "x"
        denial.diagnosis = "y"
        with patch.object(
            tools, "find_guidance_for_denial", new_callable=AsyncMock
        ) as mock_find:
            result = await tools._find_context_for_denial(denial)
            mock_find.assert_not_called()
        assert result == ""

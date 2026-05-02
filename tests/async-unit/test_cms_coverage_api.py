"""Tests for the CMS Medicare Coverage Database client and citations helper.

These tests mock httpx so they exercise the full parsing/matching/framing
logic without hitting the real CMS API.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fighthealthinsurance.cms_coverage_api import (
    CMSCoverageClient,
    NCDDocument,
    _candidate_keywords,
    _filter_by_keyword,
    _parse_ncd_record,
    _tokenize,
    get_cms_coverage_citations,
)


def _make_response(status_code: int, json_payload: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_payload
    response.text = ""
    return response


def _ncd_payload(records: list) -> dict:
    return {
        "meta": {
            "status": {"id": 200, "message": "OK"},
            "next_token": "",
        },
        "data": records,
    }


def _ncd_record(
    document_id: int = 220, display_id: str = "220.5", title: str = "MRI"
) -> dict:
    return {
        "document_id": document_id,
        "document_version": 1,
        "document_display_id": display_id,
        "last_updated": "01/01/2024",
        "document_type": "NCD",
        "title": title,
        "chapter": "220",
        "is_lab": 0,
        "url": f"/data/ncd?ncdid={document_id}&ncdver=1",
    }


class TestNCDDocument:
    def test_public_url_prepends_mcd_host(self):
        doc = NCDDocument(
            document_id=108,
            document_display_id="100.3",
            title="Esophageal pH",
            url_path="/data/ncd?ncdid=108&ncdver=1",
        )
        assert doc.public_url == (
            "https://www.cms.gov/medicare-coverage-database"
            "/data/ncd?ncdid=108&ncdver=1"
        )

    def test_public_url_handles_missing_leading_slash(self):
        doc = NCDDocument(
            document_id=1,
            document_display_id="x",
            title="t",
            url_path="data/ncd?ncdid=1",
        )
        assert doc.public_url.endswith("/data/ncd?ncdid=1")

    def test_public_url_blank_when_no_path(self):
        doc = NCDDocument(document_id=1, document_display_id="x", title="t")
        assert doc.public_url == ""

    def test_as_citation_includes_display_id_and_title(self):
        doc = NCDDocument(
            document_id=220,
            document_display_id="220.5",
            title="Magnetic Resonance Imaging",
            url_path="/data/ncd?ncdid=220&ncdver=1",
        )
        text = doc.as_citation(medicare_plan=False)
        assert "220.5" in text
        assert "Magnetic Resonance Imaging" in text
        assert "Medicare coverage policy" in text
        # We deliberately don't claim the NCD recognizes the service as
        # covered — NCDs can be non-coverage decisions.
        assert "recognizing this service as covered" not in text
        assert "must follow" not in text

    def test_as_citation_uses_binding_language_for_medicare_plans(self):
        doc = NCDDocument(document_id=220, document_display_id="220.5", title="MRI")
        text = doc.as_citation(medicare_plan=True)
        assert "bound" in text or "must follow" in text


class TestParseNCDRecord:
    def test_parses_valid_record(self):
        doc = _parse_ncd_record(_ncd_record())
        assert doc is not None
        assert doc.document_id == 220
        assert doc.document_display_id == "220.5"
        assert doc.title == "MRI"

    def test_returns_none_for_missing_id(self):
        record = _ncd_record()
        del record["document_id"]
        assert _parse_ncd_record(record) is None

    def test_returns_none_for_missing_title(self):
        record = _ncd_record()
        record["title"] = ""
        assert _parse_ncd_record(record) is None

    def test_returns_none_for_non_int_id(self):
        record = _ncd_record()
        record["document_id"] = "not-a-number"
        assert _parse_ncd_record(record) is None


class TestCandidateKeywords:
    def test_dedupes_case_insensitively(self):
        assert _candidate_keywords("MRI", "mri") == ["MRI"]

    def test_preserves_order(self):
        assert _candidate_keywords("MRI", "Cancer") == ["MRI", "Cancer"]

    def test_skips_empty(self):
        assert _candidate_keywords("", None) == []
        assert _candidate_keywords("  ", "  ") == []


class TestTokenize:
    def test_drops_stopwords(self):
        assert _tokenize("MRI of the Brain") == ["mri", "brain"]

    def test_lowercases(self):
        assert _tokenize("Cancer") == ["cancer"]

    def test_drops_short_tokens(self):
        # length-1 alphanumeric should be dropped
        assert "a" not in _tokenize("a brain")

    def test_handles_punctuation(self):
        assert _tokenize("MRI/CT, brain") == ["mri", "ct", "brain"]


class TestFilterByKeyword:
    def _docs(self, *titles):
        return [
            NCDDocument(document_id=i, document_display_id=str(i), title=t)
            for i, t in enumerate(titles, start=1)
        ]

    def test_multi_word_input_matches_titles_with_overlapping_tokens(self):
        # The original substring matcher would have failed: "MRI brain"
        # is not a substring of "MRI of the Brain".
        docs = self._docs(
            "Acupuncture",
            "MRI of the Brain",
            "MRI for Spinal Cord",
            "Magnetic Resonance Imaging — Brain",
        )
        results = _filter_by_keyword(docs, "MRI brain", max_results=5)
        titles = [d.title for d in results]
        assert "MRI of the Brain" in titles
        assert "Acupuncture" not in titles

    def test_ranks_higher_token_overlap_first(self):
        docs = self._docs(
            "Brain Imaging",  # 1 match for "MRI brain"
            "MRI of the Brain",  # 2 matches
        )
        results = _filter_by_keyword(docs, "MRI brain", max_results=2)
        assert results[0].title == "MRI of the Brain"

    def test_returns_empty_when_no_useful_tokens(self):
        docs = self._docs("MRI Brain")
        # only stopwords / tokens too short
        assert _filter_by_keyword(docs, "of the", max_results=5) == []

    def test_returns_empty_for_blank_input(self):
        docs = self._docs("MRI Brain")
        assert _filter_by_keyword(docs, "", max_results=5) == []


class TestCMSCoverageClientSearch:
    @pytest.mark.asyncio
    async def test_search_ncds_filters_by_title_keyword(self):
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(
            return_value=_make_response(
                200,
                _ncd_payload(
                    [
                        _ncd_record(
                            document_id=11, display_id="30.3", title="Acupuncture"
                        ),
                        _ncd_record(
                            document_id=220,
                            display_id="220.6",
                            title="MRI of the Brain",
                        ),
                        _ncd_record(
                            document_id=222,
                            display_id="220.5",
                            title="MRI for Spinal Cord",
                        ),
                    ]
                ),
            )
        )
        client._client = mock_http

        results = await client.search_ncds("mri", max_results=5)

        assert {r.document_id for r in results} == {220, 222}

    @pytest.mark.asyncio
    async def test_search_ncds_returns_empty_for_blank_keyword(self):
        client = CMSCoverageClient(base_url="http://test.invalid")
        client._client = MagicMock()
        assert await client.search_ncds("") == []
        assert await client.search_ncds("   ") == []

    @pytest.mark.asyncio
    async def test_search_ncds_caches_report(self):
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(
            return_value=_make_response(
                200, _ncd_payload([_ncd_record(title="MRI Brain")])
            )
        )
        client._client = mock_http

        await client.search_ncds("MRI")
        await client.search_ncds("MRI")

        assert mock_http.get.call_count == 1

    @pytest.mark.asyncio
    async def test_search_ncds_returns_empty_on_http_error(self):
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(return_value=_make_response(500, {}))
        client._client = mock_http

        results = await client.search_ncds("MRI")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_ncds_returns_empty_on_timeout(self):
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        client._client = mock_http

        results = await client.search_ncds("MRI")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_ncds_serves_cached_report_during_outage(self):
        # Successful first call populates the in-process cache, expiring the
        # report_cached_at so the next call retries the network. When the
        # network call fails, we fall back to the cached report instead of
        # zeroing out citations.
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(
            return_value=_make_response(
                200, _ncd_payload([_ncd_record(title="MRI Brain")])
            )
        )
        client._client = mock_http

        first = await client.search_ncds("MRI")
        assert len(first) == 1

        # Force the report-cache TTL to expire and simulate an outage
        client._ncd_report_cached_at = 0.0
        mock_http.get = AsyncMock(side_effect=httpx.RequestError("outage"))

        second = await client.search_ncds("MRI")

        assert {d.document_id for d in second} == {d.document_id for d in first}


class TestGetCMSCoverageCitations:
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_returns_empty_when_keywords_blank(self, mock_get_client):
        result = await get_cms_coverage_citations(None, None)
        assert result == []
        mock_get_client.assert_not_called()

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_returns_empty_when_search_returns_nothing(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.search_ncds = AsyncMock(return_value=[])
        mock_get_client.return_value = mock_client

        result = await get_cms_coverage_citations("MRI", "headache")

        assert result == []

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_dedupes_across_keywords_by_document_id(self, mock_get_client):
        mock_client = MagicMock()

        ncd = NCDDocument(
            document_id=220,
            document_display_id="220.5",
            title="Magnetic Resonance Imaging",
            url_path="/data/ncd?ncdid=220&ncdver=1",
        )
        # Both procedure ("MRI") and diagnosis ("imaging") return the same NCD
        mock_client.search_ncds = AsyncMock(return_value=[ncd])
        mock_get_client.return_value = mock_client

        citations = await get_cms_coverage_citations(
            procedure="MRI", diagnosis="imaging", max_results=5
        )

        assert len(citations) == 1

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_medicare_plan_uses_binding_language(self, mock_get_client):
        mock_client = MagicMock()
        ncd = NCDDocument(
            document_id=220,
            document_display_id="220.5",
            title="MRI",
        )
        mock_client.search_ncds = AsyncMock(return_value=[ncd])
        mock_get_client.return_value = mock_client

        commercial = await get_cms_coverage_citations("MRI", None, medicare_plan=False)
        medicare = await get_cms_coverage_citations("MRI", None, medicare_plan=True)

        assert "bound" in medicare[0]
        assert "bound" not in commercial[0]

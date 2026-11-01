"""Tests for the CMS Medicare Coverage Database client and citations helper.

These tests mock httpx so they exercise the full parsing/matching/framing
logic without hitting the real CMS API.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fighthealthinsurance.cms_coverage_api import (
    CMSCoverageClient,
    NCDDocument,
    _candidate_keywords,
    NCD_CITATION_MARKER,
    _filter_by_keyword,
    _meaningful_tokens,
    _singular,
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
        # Matched on title words, so offered as possibly applicable.
        assert NCD_CITATION_MARKER in text
        assert "only if it fits this case" in text
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

    def test_one_shared_word_is_not_a_match(self):
        # "air" alone pulled in an unrelated bed policy for an air
        # ambulance denial.
        docs = self._docs("Air-Fluidized Bed")
        assert _filter_by_keyword(docs, "air ambulance", max_results=5) == []

    def test_generic_medical_word_is_not_a_match(self):
        # Applied behavior analysis shares only "analysis" with this title.
        docs = self._docs("Hair Analysis")
        assert _filter_by_keyword(docs, "applied behavior analysis", 5) == []
        assert _filter_by_keyword(docs, "ABA therapy", max_results=5) == []

    def test_generic_word_dropped_query_needs_the_whole_title(self):
        # "speech therapy" is just "speech" once "therapy" is dropped; a
        # title with more than that subject is not a match.
        docs = self._docs("Speech Generating Devices")
        assert _filter_by_keyword(docs, "speech therapy", max_results=5) == []

    def test_title_named_in_full_by_the_query_matches(self):
        docs = self._docs("Ambulance Services", "Air-Fluidized Bed")
        results = _filter_by_keyword(docs, "air ambulance", max_results=5)
        assert [d.title for d in results] == ["Ambulance Services"]

    def test_genuine_match_still_found(self):
        docs = self._docs(
            "Hyperbaric Oxygen Therapy",
            "Oxygen for Home Use",
            "Hair Analysis",
        )
        results = _filter_by_keyword(
            docs, "hyperbaric oxygen therapy for diabetic wounds", max_results=5
        )
        assert [d.title for d in results] == ["Hyperbaric Oxygen Therapy"]

    def test_long_query_finds_a_short_title_it_mostly_names(self):
        docs = self._docs(
            "Cardiac Rehabilitation Programs",  # two of its three words
            "Knee Replacement Surgery",  # only "replacement"; surgery is generic
            "Total Joint Replacement Surgery of the Hip",  # two of four
        )
        results = _filter_by_keyword(
            docs,
            "cardiac rehabilitation after valve replacement surgery",
            max_results=5,
        )
        assert [d.title for d in results] == ["Cardiac Rehabilitation Programs"]

    def test_ranks_by_share_of_the_query_covered(self):
        docs = self._docs(
            "MRI",  # all of the title, half the query
            "MRI of the Brain",  # all of the query
        )
        results = _filter_by_keyword(docs, "MRI brain", max_results=2)
        assert [d.title for d in results] == ["MRI of the Brain", "MRI"]

    def test_plural_and_singular_count_as_the_same_word(self):
        docs = self._docs("Knee Replacement Surgery")
        results = _filter_by_keyword(docs, "knee replacements", max_results=5)
        assert [d.title for d in results] == ["Knee Replacement Surgery"]

    def test_one_word_query_needs_a_title_of_that_word(self):
        docs = self._docs("Speech Generating Devices", "Speech")
        results = _filter_by_keyword(docs, "speech", max_results=5)
        assert [d.title for d in results] == ["Speech"]

    def test_exactly_half_of_the_query_is_not_most(self):
        # Two of four query words, and two of four title words.
        docs = self._docs("Total Joint Replacement Surgery of the Hip")
        results = _filter_by_keyword(
            docs, "shoulder joint replacement revision", max_results=5
        )
        assert results == []


class TestSingular:
    def test_plain_plurals(self):
        assert _singular("replacements") == "replacement"
        assert _singular("therapies") == "therapy"
        assert _singular("patches") == "patch"
        assert _singular("x") == "x"

    def test_singular_words_ending_in_s_are_kept(self):
        for word in ("analysis", "diabetes", "lupus", "bypass", "pancreas"):
            assert _singular(word) == word

    def test_irregular_words(self):
        assert _singular("lens") == "lens"
        assert _singular("lenses") == "lens"
        assert _singular("axes") == "axis"
        assert _singular("process") == "process"
        assert _singular("processes") == "process"

    def test_lens_and_lenses_match(self):
        docs = [
            NCDDocument(document_id=1, document_display_id="1", title="Intraocular Lenses")
        ]
        results = _filter_by_keyword(docs, "intraocular lens", max_results=5)
        assert [d.title for d in results] == ["Intraocular Lenses"]

    def test_generic_words_are_dropped_in_either_form(self):
        assert _meaningful_tokens("therapies and treatments") == []


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

        results = await client.search_ncds("MRI brain", max_results=5)

        assert {r.document_id for r in results} == {220}

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

        first = await client.search_ncds("MRI brain")
        assert len(first) == 1

        # Force the report-cache TTL to expire and simulate an outage
        client._ncd_report_cached_at = 0.0
        mock_http.get = AsyncMock(side_effect=httpx.RequestError("outage"))

        second = await client.search_ncds("MRI brain")

        assert {d.document_id for d in second} == {d.document_id for d in first}


class TestLoadReport:
    def _client(self, payload):
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(return_value=_make_response(200, payload))
        client._client = mock_http
        return client

    @pytest.mark.asyncio
    async def test_fresh_empty_report_is_valid(self):
        client = self._client(_ncd_payload([]))
        assert await client.load_report() == ([], True)

    @pytest.mark.asyncio
    async def test_expired_report_served_in_an_outage_is_not_valid(self):
        client = self._client(_ncd_payload([_ncd_record(title="MRI Brain")]))
        records, valid = await client.load_report()
        assert valid and len(records) == 1
        # Past its TTL, as when a refresh failed and the old report is served.
        client._ncd_report_cached_at = time.monotonic() - client._NCD_REPORT_TTL - 1
        client._client.get = AsyncMock(side_effect=httpx.RequestError("outage"))
        records, valid = await client.load_report()
        assert len(records) == 1
        assert not valid

    @pytest.mark.asyncio
    async def test_malformed_response_is_not_a_report(self):
        client = self._client({"meta": {}})
        assert await client.load_report() == ([], False)
        assert client._ncd_report_cache is None

    @pytest.mark.asyncio
    async def test_report_whose_records_all_fail_to_parse_is_not_a_report(self):
        client = self._client({"data": [{"title": "no id"}, {"document_id": 3}]})
        assert await client.load_report() == ([], False)
        assert client._ncd_report_cache is None


class TestPartlyParsedReport:
    @pytest.mark.asyncio
    async def test_report_that_lost_a_record_is_searched_but_incomplete(self):
        # One valid unrelated record, and a relevant one with no document_id.
        payload = {
            "data": [
                _ncd_record(document_id=11, title="Acupuncture"),
                {"title": "Hyperbaric Oxygen", "document_display_id": "20.29"},
            ]
        }
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(return_value=_make_response(200, payload))
        client._client = mock_http

        with patch(
            "fighthealthinsurance.cms_coverage_api.get_cms_coverage_client",
            return_value=client,
        ):
            fresh = await get_cms_coverage_citations("hyperbaric oxygen", None)
            from_cache = await get_cms_coverage_citations("hyperbaric oxygen", None)

        assert mock_http.get.await_count == 1  # the second came from the cache
        for result in (fresh, from_cache):
            assert result == []
            assert not result.complete


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
        mock_client.load_report = AsyncMock(return_value=([], True))
        mock_get_client.return_value = mock_client

        result = await get_cms_coverage_citations("MRI", "headache")

        assert result == []
        assert result.complete

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
        # Both the procedure and the diagnosis match the same NCD.
        mock_client.load_report = AsyncMock(return_value=([ncd], True))
        mock_get_client.return_value = mock_client

        citations = await get_cms_coverage_citations(
            procedure="magnetic resonance imaging",
            diagnosis="resonance imaging",
            max_results=5,
        )

        assert len(citations) == 1

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_one_report_serves_both_keywords(self, mock_get_client):
        # The report is loaded once, so a failure can't fall on the
        # procedure search alone while the diagnosis search succeeds.
        mock_client = MagicMock()
        ncd = NCDDocument(
            document_id=11, document_display_id="20.29", title="Hyperbaric Oxygen"
        )
        mock_client.load_report = AsyncMock(return_value=([ncd], True))
        mock_get_client.return_value = mock_client

        result = await get_cms_coverage_citations(
            "hyperbaric oxygen", "diabetic foot ulcer"
        )

        mock_client.load_report.assert_awaited_once()
        assert len(result) == 1
        assert result.complete

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_lookup_on_an_invalid_report_is_incomplete(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.load_report = AsyncMock(return_value=([], False))
        mock_get_client.return_value = mock_client

        result = await get_cms_coverage_citations("MRI brain", "headache")

        assert result == []
        assert not result.complete

    @pytest.mark.asyncio
    async def test_failed_procedure_fetch_cannot_store_an_empty_result(self):
        # Path A end to end: the first fetch fails, then the report comes
        # back. Before, the procedure search hit the failure and the
        # diagnosis search the good report, and [] looked finished.
        client = CMSCoverageClient(base_url="http://test.invalid")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.get = AsyncMock(
            side_effect=[
                httpx.RequestError("outage"),
                _make_response(
                    200, _ncd_payload([_ncd_record(title="Hyperbaric Oxygen")])
                ),
            ]
        )
        client._client = mock_http
        with patch(
            "fighthealthinsurance.cms_coverage_api.get_cms_coverage_client",
            return_value=client,
        ):
            first = await get_cms_coverage_citations(
                "hyperbaric oxygen", "diabetic foot ulcer"
            )
            second = await get_cms_coverage_citations(
                "hyperbaric oxygen", "diabetic foot ulcer"
            )

        assert first == [] and not first.complete
        assert len(second) == 1 and second.complete

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.cms_coverage_api.get_cms_coverage_client")
    async def test_medicare_plan_uses_binding_language(self, mock_get_client):
        mock_client = MagicMock()
        ncd = NCDDocument(
            document_id=220,
            document_display_id="220.5",
            title="MRI",
        )
        mock_client.load_report = AsyncMock(return_value=([ncd], True))
        mock_get_client.return_value = mock_client

        commercial = await get_cms_coverage_citations("MRI", None, medicare_plan=False)
        medicare = await get_cms_coverage_citations("MRI", None, medicare_plan=True)

        assert "bound" in medicare[0]
        assert "bound" not in commercial[0]

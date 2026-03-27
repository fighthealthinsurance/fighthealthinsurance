"""
Unit tests for the aggressive PDF URL resolution methods in PubMedTools.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance.pubmed_tools import PubMedTools

@pytest.fixture
def tools():
    return PubMedTools()

def _make_mock_response(
    status=200,
    json_data=None,
    text_data="",
    content=b"",
    content_type="text/html",
    url="https://example.com",
):
    """Create a mock aiohttp response with async context manager support."""
    resp = AsyncMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data)
    resp.read = AsyncMock(return_value=content)
    resp.url = url
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    # Support async context manager (async with session.get(...) as resp)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp

def _make_mock_session(responses=None):
    """Create a mock aiohttp.ClientSession.

    Args:
        responses: list of mock responses to return in order from get/head calls,
                   or a single response for all calls.
    """
    session = AsyncMock()
    if responses is None:
        responses = [_make_mock_response()]

    if isinstance(responses, list):
        # Return responses in order; if we run out, use the last one
        call_count = {"get": 0, "head": 0}

        def make_get(*args, **kwargs):
            idx = min(call_count["get"], len(responses) - 1)
            call_count["get"] += 1
            return responses[idx]

        def make_head(*args, **kwargs):
            idx = min(call_count["head"], len(responses) - 1)
            call_count["head"] += 1
            return responses[idx]

        session.get = MagicMock(side_effect=make_get)
        session.head = MagicMock(side_effect=make_head)
    else:
        session.get = MagicMock(return_value=responses)
        session.head = MagicMock(return_value=responses)

    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session

class TestExtractPdfUrlFromHtml:
    """Tests for _extract_pdf_url_from_html static method."""

    def test_citation_pdf_url_name_first(self):
        html = '<meta name="citation_pdf_url" content="https://example.com/paper.pdf">'
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://example.com/article"
        )
        assert result == "https://example.com/paper.pdf"

    def test_citation_pdf_url_content_first(self):
        html = '<meta content="https://example.com/paper.pdf" name="citation_pdf_url">'
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://example.com/article"
        )
        assert result == "https://example.com/paper.pdf"

    def test_pdf_anchor_tag(self):
        html = '<a href="/downloads/paper.pdf" class="btn">Download PDF</a>'
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://publisher.com/article/123"
        )
        assert result == "https://publisher.com/downloads/paper.pdf"

    def test_relative_url_resolved(self):
        html = '<meta name="citation_pdf_url" content="/pdf/12345.pdf">'
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://journals.example.com/article/view/1"
        )
        assert result == "https://journals.example.com/pdf/12345.pdf"

    def test_absolute_url_unchanged(self):
        html = (
            '<meta name="citation_pdf_url" content="https://cdn.example.com/paper.pdf">'
        )
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://different-domain.com/article"
        )
        assert result == "https://cdn.example.com/paper.pdf"

    def test_no_pdf_link_returns_none(self):
        html = "<html><body><p>No PDF here</p></body></html>"
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://example.com/article"
        )
        assert result is None

    def test_case_insensitive(self):
        html = '<META NAME="Citation_PDF_URL" CONTENT="https://example.com/paper.pdf">'
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://example.com/article"
        )
        assert result == "https://example.com/paper.pdf"

    def test_pdf_url_with_query_params(self):
        html = '<a href="/paper.pdf?token=abc123" class="link">Full Text PDF</a>'
        result = PubMedTools._extract_pdf_url_from_html(
            html, "https://example.com/article"
        )
        assert result == "https://example.com/paper.pdf?token=abc123"

@pytest.mark.asyncio
class TestTryFindit:
    """Tests for _try_findit method."""

    async def test_returns_url_on_success(self, tools):

        mock_src = MagicMock()
        mock_src.url = "https://example.com/article.pdf"

        with patch("fighthealthinsurance.pubmed_tools.sync_to_async") as mock_s2a:
            mock_s2a.return_value = AsyncMock(return_value=mock_src)
            result = await tools._try_findit("12345", timeout_secs=5.0)

        assert result == "https://example.com/article.pdf"

    async def test_returns_none_on_exception(self, tools):

        with patch("fighthealthinsurance.pubmed_tools.sync_to_async") as mock_s2a:
            mock_s2a.return_value = AsyncMock(side_effect=Exception("FindIt error"))
            result = await tools._try_findit("12345", timeout_secs=5.0)

        assert result is None

    async def test_returns_none_when_url_is_none(self, tools):

        mock_src = MagicMock()
        mock_src.url = None

        with patch("fighthealthinsurance.pubmed_tools.sync_to_async") as mock_s2a:
            mock_s2a.return_value = AsyncMock(return_value=mock_src)
            result = await tools._try_findit("12345", timeout_secs=5.0)

        assert result is None

@pytest.mark.asyncio
class TestTryPmc:
    """Tests for _try_pmc method."""

    async def test_returns_pdf_url_when_pmcid_found(self, tools):

        converter_resp = _make_mock_response(
            json_data={"records": [{"pmcid": "PMC1234567"}]}
        )
        session = _make_mock_session(responses=[converter_resp])

        result = await tools._try_pmc("12345", session, timeout_secs=5.0)
        assert result == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/pdf/"

    async def test_returns_none_when_no_pmcid(self, tools):

        converter_resp = _make_mock_response(json_data={"records": [{"pmid": "12345"}]})
        session = _make_mock_session(responses=[converter_resp])

        result = await tools._try_pmc("12345", session, timeout_secs=5.0)
        assert result is None

    async def test_returns_none_when_empty_records(self, tools):

        converter_resp = _make_mock_response(json_data={"records": []})
        session = _make_mock_session(responses=[converter_resp])

        result = await tools._try_pmc("12345", session, timeout_secs=5.0)
        assert result is None

    async def test_returns_none_on_network_error(self, tools):

        session = AsyncMock()
        session.get = MagicMock(side_effect=Exception("Connection refused"))

        result = await tools._try_pmc("12345", session, timeout_secs=5.0)
        assert result is None

@pytest.mark.asyncio
class TestTryEuropePmc:
    """Tests for _try_europe_pmc method."""

    async def test_returns_pdf_url_when_oa_pdf_found(self, tools):

        resp = _make_mock_response(
            json_data={
                "resultList": {
                    "result": [
                        {
                            "fullTextUrlList": {
                                "fullTextUrl": [
                                    {
                                        "documentStyle": "pdf",
                                        "availabilityCode": "OA",
                                        "url": "https://europepmc.org/article/pdf/12345",
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_europe_pmc("12345", session, timeout_secs=5.0)
        assert result == "https://europepmc.org/article/pdf/12345"

    async def test_falls_back_to_non_pdf_oa_url(self, tools):

        resp = _make_mock_response(
            json_data={
                "resultList": {
                    "result": [
                        {
                            "fullTextUrlList": {
                                "fullTextUrl": [
                                    {
                                        "documentStyle": "html",
                                        "availabilityCode": "OA",
                                        "url": "https://europepmc.org/article/12345",
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_europe_pmc("12345", session, timeout_secs=5.0)
        assert result == "https://europepmc.org/article/12345"

    async def test_returns_none_when_no_results(self, tools):

        resp = _make_mock_response(json_data={"resultList": {"result": []}})
        session = _make_mock_session(responses=[resp])

        result = await tools._try_europe_pmc("12345", session, timeout_secs=5.0)
        assert result is None

    async def test_returns_none_when_no_oa_urls(self, tools):

        resp = _make_mock_response(
            json_data={
                "resultList": {
                    "result": [
                        {
                            "fullTextUrlList": {
                                "fullTextUrl": [
                                    {
                                        "documentStyle": "pdf",
                                        "availabilityCode": "S",
                                        "url": "https://publisher.com/paywalled.pdf",
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_europe_pmc("12345", session, timeout_secs=5.0)
        assert result is None

@pytest.mark.asyncio
class TestTryUnpaywall:
    """Tests for _try_unpaywall method."""

    async def test_returns_best_oa_pdf_url(self, tools):

        resp = _make_mock_response(
            json_data={
                "best_oa_location": {
                    "url_for_pdf": "https://unpaywall.org/pdf/10.1000/test",
                    "url_for_landing_page": "https://publisher.com/article",
                },
                "oa_locations": [],
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_unpaywall("10.1000/test", session, timeout_secs=5.0)
        assert result == "https://unpaywall.org/pdf/10.1000/test"

    async def test_falls_back_to_landing_page(self, tools):

        resp = _make_mock_response(
            json_data={
                "best_oa_location": {
                    "url_for_pdf": None,
                    "url_for_landing_page": "https://publisher.com/article",
                },
                "oa_locations": [],
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_unpaywall("10.1000/test", session, timeout_secs=5.0)
        assert result == "https://publisher.com/article"

    async def test_falls_back_to_oa_locations(self, tools):

        resp = _make_mock_response(
            json_data={
                "best_oa_location": None,
                "oa_locations": [
                    {"url_for_pdf": "https://repo.org/pdf/article.pdf"},
                ],
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_unpaywall("10.1000/test", session, timeout_secs=5.0)
        assert result == "https://repo.org/pdf/article.pdf"

    async def test_returns_none_on_404(self, tools):

        resp = _make_mock_response(status=404)
        session = _make_mock_session(responses=[resp])

        result = await tools._try_unpaywall("10.1000/test", session, timeout_secs=5.0)
        assert result is None

    async def test_returns_none_when_no_oa(self, tools):

        resp = _make_mock_response(
            json_data={
                "best_oa_location": None,
                "oa_locations": [],
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_unpaywall("10.1000/test", session, timeout_secs=5.0)
        assert result is None

@pytest.mark.asyncio
class TestTryPreprintServers:
    """Tests for _try_preprint_servers method."""

    async def test_returns_pdf_for_medrxiv_doi_with_jatsxml(self, tools):

        resp = _make_mock_response(
            json_data={
                "collection": [
                    {
                        "doi": "10.1101/2024.01.01.123456",
                        "jatsxml": "https://www.medrxiv.org/content/10.1101/2024.01.01.123456v1.source.xml",
                    }
                ]
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_preprint_servers(
            "10.1101/2024.01.01.123456", session, timeout_secs=5.0
        )
        assert (
            result
            == "https://www.medrxiv.org/content/10.1101/2024.01.01.123456v1.full.pdf"
        )

    async def test_constructs_url_from_doi_when_no_jatsxml(self, tools):

        resp = _make_mock_response(
            json_data={
                "collection": [
                    {
                        "doi": "10.1101/2024.01.01.123456",
                    }
                ]
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_preprint_servers(
            "10.1101/2024.01.01.123456", session, timeout_secs=5.0
        )
        assert (
            result
            == "https://www.medrxiv.org/content/10.1101/2024.01.01.123456.full.pdf"
        )

    async def test_non_preprint_doi_uses_pubs_endpoint(self, tools):

        # /pubs/ endpoint returns no results for this DOI
        resp = _make_mock_response(json_data={"collection": []})
        session = _make_mock_session(responses=[resp])

        result = await tools._try_preprint_servers(
            "10.1016/j.cell.2024.01.001", session, timeout_secs=5.0
        )
        assert result is None
        # Should have made exactly 1 call (medrxiv pubs endpoint only)
        assert session.get.call_count == 1

    async def test_non_preprint_doi_finds_preprint(self, tools):

        resp = _make_mock_response(
            json_data={
                "collection": [
                    {
                        "preprint_doi": "10.1101/2023.06.15.545100",
                    }
                ]
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_preprint_servers(
            "10.1016/j.cell.2024.01.001", session, timeout_secs=5.0
        )
        assert (
            result
            == "https://www.medrxiv.org/content/10.1101/2023.06.15.545100.full.pdf"
        )

    async def test_biorxiv_doi_pattern(self, tools):

        resp = _make_mock_response(
            json_data={
                "collection": [
                    {
                        "doi": "10.1101/2024.05.15.789012",
                    }
                ]
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_preprint_servers(
            "10.1101/2024.05.15.789012", session, timeout_secs=5.0
        )
        # Should match medrxiv first due to 10.1101/ pattern
        assert result is not None
        assert "10.1101/2024.05.15.789012.full.pdf" in result

@pytest.mark.asyncio
class TestQueryBiorxivApi:
    """Tests for _query_biorxiv_api helper method."""

    async def test_returns_pdf_url_from_jatsxml(self, tools):

        resp = _make_mock_response(
            json_data={
                "collection": [
                    {
                        "doi": "10.1101/2024.01.01.123456",
                        "jatsxml": "https://www.medrxiv.org/content/10.1101/2024.01.01.123456v1.source.xml",
                    }
                ]
            }
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._query_biorxiv_api(
            "medrxiv", "10.1101/2024.01.01.123456", session, timeout_secs=5.0
        )
        assert (
            result
            == "https://www.medrxiv.org/content/10.1101/2024.01.01.123456v1.full.pdf"
        )

    async def test_returns_pdf_url_from_doi_when_no_jatsxml(self, tools):

        resp = _make_mock_response(
            json_data={"collection": [{"doi": "10.1101/2024.01.01.123456"}]}
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._query_biorxiv_api(
            "biorxiv", "10.1101/2024.01.01.123456", session, timeout_secs=5.0
        )
        assert (
            result
            == "https://www.biorxiv.org/content/10.1101/2024.01.01.123456.full.pdf"
        )

    async def test_returns_none_on_empty_collection(self, tools):

        resp = _make_mock_response(json_data={"collection": []})
        session = _make_mock_session(responses=[resp])

        result = await tools._query_biorxiv_api(
            "medrxiv", "10.1101/nothing", session, timeout_secs=5.0
        )
        assert result is None

    async def test_returns_none_on_error(self, tools):

        session = AsyncMock()
        session.get = MagicMock(side_effect=Exception("timeout"))

        result = await tools._query_biorxiv_api(
            "medrxiv", "10.1101/err", session, timeout_secs=5.0
        )
        assert result is None

@pytest.mark.asyncio
class TestQueryBiorxivPubsApi:
    """Tests for _query_biorxiv_pubs_api helper method."""

    async def test_returns_pdf_url_when_preprint_found(self, tools):

        resp = _make_mock_response(
            json_data={"collection": [{"preprint_doi": "10.1101/2023.06.15.545100"}]}
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._query_biorxiv_pubs_api(
            "medrxiv", "10.1016/j.cell.2024.01.001", session, timeout_secs=5.0
        )
        assert (
            result
            == "https://www.medrxiv.org/content/10.1101/2023.06.15.545100.full.pdf"
        )

    async def test_returns_none_on_empty_collection(self, tools):

        resp = _make_mock_response(json_data={"collection": []})
        session = _make_mock_session(responses=[resp])

        result = await tools._query_biorxiv_pubs_api(
            "medrxiv", "10.1016/j.cell.2024.01.001", session, timeout_secs=5.0
        )
        assert result is None

    async def test_returns_none_on_error(self, tools):

        session = AsyncMock()
        session.get = MagicMock(side_effect=Exception("timeout"))

        result = await tools._query_biorxiv_pubs_api(
            "medrxiv", "10.1016/j.cell.2024.01.001", session, timeout_secs=5.0
        )
        assert result is None

    async def test_returns_none_when_no_preprint_doi(self, tools):

        resp = _make_mock_response(
            json_data={"collection": [{"some_other_field": "value"}]}
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._query_biorxiv_pubs_api(
            "medrxiv", "10.1016/j.cell.2024.01.001", session, timeout_secs=5.0
        )
        assert result is None

class TestIsPdfResponse:
    """Tests for _is_pdf_response static method."""

    def test_pdf_in_url(self):
        assert PubMedTools._is_pdf_response(
            "https://example.com/paper.pdf", "text/html"
        )

    def test_pdf_content_type(self):
        assert PubMedTools._is_pdf_response(
            "https://example.com/download", "application/pdf"
        )

    def test_neither(self):
        assert not PubMedTools._is_pdf_response("https://example.com/page", "text/html")

    def test_pdf_url_with_query(self):
        assert PubMedTools._is_pdf_response(
            "https://example.com/paper.pdf?token=abc", "text/html"
        )

@pytest.mark.asyncio
class TestTryDoiResolution:
    """Tests for _try_doi_resolution method."""

    async def test_returns_url_when_doi_resolves_to_pdf(self, tools):

        resp = _make_mock_response(
            status=200,
            content_type="application/pdf",
            url="https://publisher.com/article/12345.pdf",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_doi_resolution(
            "10.1000/test", session, timeout_secs=5.0
        )
        assert result == "https://publisher.com/article/12345.pdf"

    async def test_extracts_pdf_from_html_landing_page(self, tools):

        html = '<html><head><meta name="citation_pdf_url" content="https://publisher.com/pdf/12345.pdf"></head></html>'
        resp = _make_mock_response(
            status=200,
            content_type="text/html; charset=utf-8",
            text_data=html,
            url="https://publisher.com/article/12345",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_doi_resolution(
            "10.1000/test", session, timeout_secs=5.0
        )
        assert result == "https://publisher.com/pdf/12345.pdf"

    async def test_returns_none_when_no_pdf_on_page(self, tools):

        resp = _make_mock_response(
            status=200,
            content_type="text/html",
            text_data="<html><body>No PDF link here</body></html>",
            url="https://publisher.com/article/12345",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_doi_resolution(
            "10.1000/test", session, timeout_secs=5.0
        )
        assert result is None

    async def test_returns_none_on_404(self, tools):

        resp = _make_mock_response(status=404)
        session = _make_mock_session(responses=[resp])

        result = await tools._try_doi_resolution(
            "10.1000/nonexistent", session, timeout_secs=5.0
        )
        assert result is None

@pytest.mark.asyncio
class TestFindArticleUrl:
    """Tests for the _find_article_url fallback chain."""

    async def test_returns_findit_url_first(self, tools):

        session = _make_mock_session()

        with patch.object(tools, "_try_findit", new_callable=AsyncMock) as mock_findit:
            mock_findit.return_value = "https://findit.com/article.pdf"

            result = await tools._find_article_url(
                "12345", doi="10.1000/test", session=session
            )

        assert result == "https://findit.com/article.pdf"

    async def test_falls_through_to_pmc_when_findit_fails(self, tools):

        session = _make_mock_session()

        with patch.object(
            tools, "_try_findit", new_callable=AsyncMock, return_value=None
        ), patch.object(tools, "_try_pmc", new_callable=AsyncMock) as mock_pmc:
            mock_pmc.return_value = "https://pmc.ncbi.nlm.nih.gov/article.pdf"

            result = await tools._find_article_url(
                "12345", doi="10.1000/test", session=session
            )

        assert result == "https://pmc.ncbi.nlm.nih.gov/article.pdf"

    async def test_falls_through_entire_chain(self, tools):

        session = _make_mock_session()

        with patch.object(
            tools, "_try_findit", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_pmc", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_europe_pmc", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_unpaywall", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_preprint_servers", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools,
            "_try_doi_resolution",
            new_callable=AsyncMock,
        ) as mock_doi:
            mock_doi.return_value = "https://publisher.com/pdf/article.pdf"

            result = await tools._find_article_url(
                "12345", doi="10.1000/test", session=session
            )

        assert result == "https://publisher.com/pdf/article.pdf"

    async def test_returns_none_when_all_fail(self, tools):

        session = _make_mock_session()

        with patch.object(
            tools, "_try_findit", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_pmc", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_europe_pmc", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_unpaywall", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_preprint_servers", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_doi_resolution", new_callable=AsyncMock, return_value=None
        ):
            result = await tools._find_article_url(
                "12345", doi="10.1000/test", session=session
            )

        assert result is None

    async def test_skips_doi_sources_when_no_doi(self, tools):

        session = _make_mock_session()

        with patch.object(
            tools, "_try_findit", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_pmc", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_europe_pmc", new_callable=AsyncMock, return_value=None
        ), patch.object(
            tools, "_try_unpaywall", new_callable=AsyncMock
        ) as mock_unpaywall, patch.object(
            tools, "_try_preprint_servers", new_callable=AsyncMock
        ) as mock_preprint, patch.object(
            tools, "_try_doi_resolution", new_callable=AsyncMock
        ) as mock_doi:
            result = await tools._find_article_url("12345", doi=None, session=session)

        # DOI-dependent methods should not be called
        mock_unpaywall.assert_not_called()
        mock_preprint.assert_not_called()
        mock_doi.assert_not_called()
        assert result is None

    async def test_creates_own_session_when_none_provided(self, tools):

        with patch.object(
            tools, "_try_findit", new_callable=AsyncMock
        ) as mock_findit, patch(
            "fighthealthinsurance.pubmed_tools.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_findit.return_value = "https://example.com/article.pdf"
            mock_session = AsyncMock()
            mock_session.close = AsyncMock()
            mock_session_cls.return_value = mock_session

            result = await tools._find_article_url("12345")

        assert result == "https://example.com/article.pdf"
        mock_session_cls.assert_called_once()
        mock_session.close.assert_called_once()

@pytest.mark.asyncio
class TestFetchTextFromUrl:
    """Tests for _fetch_text_from_url method."""

    async def test_extracts_text_from_html_response(self, tools):

        resp = _make_mock_response(
            text_data="This is a long enough article text that should be returned as content for the test",
            content_type="text/html",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._fetch_text_from_url(
            "https://example.com/article.html", session
        )
        assert "long enough article text" in result

    async def test_returns_empty_string_on_short_text(self, tools):

        resp = _make_mock_response(
            text_data="short",
            content_type="text/html",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._fetch_text_from_url(
            "https://example.com/article.html", session
        )
        assert result == ""

    async def test_returns_empty_string_on_error(self, tools):

        session = AsyncMock()
        error_resp = AsyncMock()
        error_resp.__aenter__ = AsyncMock(side_effect=Exception("Connection error"))
        error_resp.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=error_resp)

        result = await tools._fetch_text_from_url("https://example.com/broken", session)
        assert result == ""

@pytest.mark.asyncio
class TestTryFetchPdfToFile:
    """Tests for _try_fetch_pdf_to_file method."""

    async def test_saves_pdf_to_temp_file(self, tools):

        pdf_content = b"%PDF-1.4 " + b"x" * 100  # >20 bytes
        resp = _make_mock_response(
            content=pdf_content,
            content_type="application/pdf",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_fetch_pdf_to_file(
            "https://example.com/paper.pdf", "test_", session
        )
        assert result is not None
        assert result.endswith(".pdf")

        # Verify content was written
        with open(result, "rb") as f:
            assert f.read() == pdf_content

    async def test_returns_none_for_non_pdf_content(self, tools):

        resp = _make_mock_response(
            content=b"<html>not a pdf</html>",
            content_type="text/html",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_fetch_pdf_to_file(
            "https://example.com/article.html", "test_", session
        )
        assert result is None

    async def test_returns_none_for_tiny_content(self, tools):

        resp = _make_mock_response(
            content=b"tiny",  # <=20 bytes
            content_type="application/pdf",
        )
        session = _make_mock_session(responses=[resp])

        result = await tools._try_fetch_pdf_to_file(
            "https://example.com/paper.pdf", "test_", session
        )
        assert result is None

    async def test_returns_none_on_non_200(self, tools):

        resp = _make_mock_response(status=403)
        session = _make_mock_session(responses=[resp])

        result = await tools._try_fetch_pdf_to_file(
            "https://example.com/paper.pdf", "test_", session
        )
        assert result is None

    async def test_returns_none_on_network_error(self, tools):

        session = AsyncMock()
        error_resp = AsyncMock()
        error_resp.__aenter__ = AsyncMock(side_effect=Exception("Network error"))
        error_resp.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=error_resp)

        result = await tools._try_fetch_pdf_to_file(
            "https://example.com/paper.pdf", "test_", session
        )
        assert result is None

import asyncio
import io
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlencode, urljoin

import aiohttp
import eutils
import PyPDF2
from asgiref.sync import async_to_sync, sync_to_async
from loguru import logger
from metapub import FindIt

from fighthealthinsurance.microsites import get_microsite
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import (
    PubMedArticleSummarized,
    PubMedMiniArticle,
    PubMedQueryData,
)
from fighthealthinsurance.pubmed_search import (
    EvidenceStrength,
    build_structured_query,
    categorize_articles_by_strength,
)
from fighthealthinsurance.utils import pubmed_fetcher

from .exec import pubmed_executor
from .models import Denial
from .utils import _try_pandoc_engines, markdown_escape

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


PER_QUERY = 2

# Single source of truth for the contact identity NCBI / Unpaywall / etc.
# expect alongside high-volume API requests. Used both in the fetch User-Agent
# and the per-API `tool`/`email` query params so the value can't drift between
# call sites.
_TOOL_NAME = "fighthealthinsurance"
_CONTACT_EMAIL = "support@fighthealthinsurance.com"

# Common headers to avoid bot detection when fetching PDFs
_FETCH_HEADERS = {
    "User-Agent": f"FightHealthInsurance/1.0 (mailto:{_CONTACT_EMAIL})",
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*",
}

# NCBI E-utilities REST API base URL. Used for elink (related articles) and
# efetch (MeSH terms / publication types). NCBI requests that ``tool`` and
# ``email`` be supplied per their usage guidelines.
_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_EUTILS_PARAMS = {
    "tool": _TOOL_NAME,
    "email": _CONTACT_EMAIL,
}

# NCBI documents 200 IDs as the practical limit for an efetch GET; chunk to
# stay well under URL-length caps regardless of caller-supplied list size.
_EFETCH_BATCH_SIZE = 200


@asynccontextmanager
async def _ensure_session(
    session: Optional[aiohttp.ClientSession],
) -> AsyncIterator[aiohttp.ClientSession]:
    """Yield ``session`` if provided, else open and close one for the caller.

    Lets methods accept an optional shared session for batching while still
    working standalone.
    """
    if session is not None:
        yield session
        return
    async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as new_session:
        yield new_session


class PubMedTools(object):
    # Rough bias to "recent" articles
    since_list = ["2024", None]

    def _url_sources(
        self,
        pmid: str,
        doi: Optional[str],
        session: aiohttp.ClientSession,
        per_source_timeout: float,
    ) -> List[Tuple[str, Any]]:
        """Build the ordered list of (name, coroutine-factory) pairs for URL resolution.

        Returns callables (not live coroutines) so the caller can create them lazily,
        avoiding "coroutine was never awaited" warnings on early return.
        """
        t = per_source_timeout
        sources: List[Tuple[str, Any]] = [
            ("FindIt", lambda: self._try_findit(pmid, t)),
            ("PMC", lambda: self._try_pmc(pmid, session, t)),
            ("Europe PMC", lambda: self._try_europe_pmc(pmid, session, t)),
        ]
        if doi:
            sources += [
                ("Unpaywall", lambda: self._try_unpaywall(doi, session, t)),
                (
                    "preprint server",
                    lambda: self._try_preprint_servers(doi, session, t),
                ),
                ("DOI resolution", lambda: self._try_doi_resolution(doi, session, t)),
            ]
        return sources

    async def _find_article_url(
        self,
        pmid: str,
        doi: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        per_source_timeout: float = 10.0,
    ) -> Optional[str]:
        """Try multiple sources in order to find a PDF/full-text URL for an article."""
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession(headers=_FETCH_HEADERS)
        assert session is not None

        try:
            for name, make_coro in self._url_sources(
                pmid, doi, session, per_source_timeout
            ):
                url: Optional[str] = await make_coro()
                if url:
                    logger.debug(f"[{pmid}] Found URL via {name}: {url}")
                    return url
            logger.debug(f"[{pmid}] No PDF URL found from any source")
            return None
        finally:
            if owns_session and session:
                await session.close()

    async def _try_findit(self, pmid: str, timeout_secs: float) -> Optional[str]:
        """Try metapub's FindIt to locate article URL."""
        try:
            src = await asyncio.wait_for(
                sync_to_async(FindIt)(pmid),
                timeout=timeout_secs,
            )
            url: Optional[str] = src.url
            return url
        except Exception:
            logger.debug(f"[{pmid}] FindIt failed")
            return None

    async def _fetch_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        timeout_secs: float,
        label: str = "",
    ) -> Optional[Dict]:
        """Fetch JSON from url with timeout. Returns None on any failure."""
        try:
            async with async_timeout(timeout_secs):
                async with session.get(url) as resp:
                    if resp.status == 200:
                        result: Dict = await resp.json()
                        return result
        except Exception as e:
            logger.debug(f"[{label}] JSON fetch failed: {e}")
        return None

    async def _try_pmc(
        self, pmid: str, session: aiohttp.ClientSession, timeout_secs: float
    ) -> Optional[str]:
        """Check if article is in PubMed Central and get its PDF URL."""
        params = urlencode({**_EUTILS_PARAMS, "ids": pmid, "format": "json"})
        url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?{params}"
        data = await self._fetch_json(session, url, timeout_secs, pmid)
        if data:
            records = data.get("records", [])
            if records and records[0].get("pmcid"):
                return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{records[0]['pmcid']}/pdf/"
        return None

    async def _try_europe_pmc(
        self, pmid: str, session: aiohttp.ClientSession, timeout_secs: float
    ) -> Optional[str]:
        """Try Europe PMC for full text PDF."""
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:{pmid}%20AND%20SRC:MED&resultType=core&format=json"
        data = await self._fetch_json(session, url, timeout_secs, pmid)
        if not data:
            return None
        results = data.get("resultList", {}).get("result", [])
        if not results:
            return None
        ft_urls = results[0].get("fullTextUrlList", {}).get("fullTextUrl", [])
        fallback = None
        for ft in ft_urls:
            if ft.get("availabilityCode") not in ("OA", "F") or not ft.get("url"):
                continue
            if ft.get("documentStyle") == "pdf":
                return str(ft["url"])
            if fallback is None:
                fallback = str(ft["url"])
        return fallback

    async def _try_unpaywall(
        self, doi: str, session: aiohttp.ClientSession, timeout_secs: float
    ) -> Optional[str]:
        """Try Unpaywall API to find open access PDF."""
        url = (
            f"https://api.unpaywall.org/v2/{quote(doi, safe='')}?email={_CONTACT_EMAIL}"
        )
        data = await self._fetch_json(session, url, timeout_secs, f"DOI:{doi}")
        if not data:
            return None
        best_oa = data.get("best_oa_location")
        if best_oa and best_oa.get("url_for_pdf"):
            return str(best_oa["url_for_pdf"])
        for oa_loc in data.get("oa_locations", []):
            if oa_loc.get("url_for_pdf"):
                return str(oa_loc["url_for_pdf"])
        if best_oa and best_oa.get("url_for_landing_page"):
            return str(best_oa["url_for_landing_page"])
        return None

    async def _query_biorxiv(
        self,
        server: str,
        doi: str,
        endpoint: str,
        session: aiohttp.ClientSession,
        timeout_secs: float,
    ) -> Optional[str]:
        """Query biorxiv/medrxiv API. endpoint is 'details' or 'pubs'."""
        url = f"https://api.biorxiv.org/{endpoint}/{server}/{doi}"
        data = await self._fetch_json(session, url, timeout_secs, f"DOI:{doi}")
        if not data:
            return None
        results = data.get("collection", [])
        if not results:
            return None
        last = results[-1]
        if endpoint == "details":
            jatsxml = last.get("jatsxml")
            if jatsxml:
                return str(jatsxml).replace(".source.xml", ".full.pdf")
            doi_val = last.get("doi") or ""
        else:
            doi_val = last.get("preprint_doi") or ""
        if doi_val:
            return f"https://www.{server}.org/content/{doi_val}.full.pdf"
        return None

    async def _try_preprint_servers(
        self, doi: str, session: aiohttp.ClientSession, timeout_secs: float
    ) -> Optional[str]:
        """Try medRxiv and bioRxiv for preprint PDFs."""
        doi_lower = doi.lower()
        is_preprint = (
            "10.1101/" in doi or "medrxiv" in doi_lower or "biorxiv" in doi_lower
        )
        if is_preprint:
            for server in ("medrxiv", "biorxiv"):
                result = await self._query_biorxiv(
                    server, doi, "details", session, timeout_secs
                )
                if result:
                    return result
        else:
            # Only try medrxiv to limit HTTP calls (most relevant for health appeals)
            result = await self._query_biorxiv(
                "medrxiv", doi, "pubs", session, timeout_secs
            )
            if result:
                return result
        return None

    async def _try_doi_resolution(
        self,
        doi: str,
        session: aiohttp.ClientSession,
        timeout_secs: float,
    ) -> Optional[str]:
        """Resolve DOI and look for PDF links on the landing page."""
        try:
            doi_url = f"https://doi.org/{quote(doi, safe='')}"
            async with async_timeout(timeout_secs):
                async with session.get(
                    doi_url, allow_redirects=True, headers=_FETCH_HEADERS
                ) as resp:
                    if resp.status == 200:
                        final_url = str(resp.url)
                        content_type = resp.headers.get("Content-Type", "")

                        if "application/pdf" in content_type:
                            return final_url

                        if "text/html" in content_type:
                            html = await resp.text()
                            pdf_url = self._extract_pdf_url_from_html(html, final_url)
                            if pdf_url:
                                return pdf_url
        except Exception as e:
            logger.debug(f"[DOI:{doi}] DOI resolution failed: {e}")
        return None

    @staticmethod
    def _is_pdf_response(url: str, content_type: str) -> bool:
        """Check if a URL or content-type indicates a PDF response."""
        if "application/pdf" in content_type.lower():
            return True
        return url.lower().split("?", 1)[0].endswith(".pdf")

    @staticmethod
    def _extract_pdf_url_from_html(html: str, base_url: str) -> Optional[str]:
        """Extract PDF URL from publisher HTML page using common patterns."""
        patterns = [
            r'<meta[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\'](.*?)["\']',
            r'<meta[^>]*content=["\'](.*?)["\'][^>]*name=["\']citation_pdf_url["\']',
            r'<a[^>]*href=["\'](.*?\.pdf(?:\?[^"\']*)?)["\'][^>]*>(?:[^<]*(?:pdf|full.text|download))',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                pdf_url = match.group(1)
                if not pdf_url.startswith("http"):
                    pdf_url = urljoin(base_url, pdf_url)
                return pdf_url
        return None

    async def find_pubmed_article_ids_for_query(
        self,
        query: str,
        since: Optional[str] = None,
        timeout: float = 60.0,
    ) -> List[str]:
        """
        Asynchronously retrieves PubMed article IDs matching a search query, optionally filtered by year.

        Checks for recent cached results in the database and returns them if available. If not cached or if parsing fails, queries the PubMed API, stores new results in the database, and returns the list of PMIDs. Returns an empty list if a timeout or cancellation occurs.

        Args:
            query: The PubMed search query string.
            since: Optional year to filter articles published since this year.
            timeout: Maximum time in seconds to spend on the operation.

        Returns:
            A list of PubMed article IDs (PMIDs) matching the query.
        """
        pmids: List[str] = []

        try:
            async with async_timeout(timeout):
                # Check if we already have query results from the last month
                month_ago = datetime.now() - timedelta(days=30)
                existing_queries = PubMedQueryData.objects.filter(
                    query=query,
                    created__gte=month_ago,
                    since=since,
                    denial_id__isnull=True,
                ).order_by("-created")

                if await existing_queries.aexists():
                    # Use cached query results
                    async for query_data in existing_queries:
                        if query_data.articles:
                            try:
                                article_ids: list[str] = json.loads(
                                    query_data.articles.replace("\x00", "")
                                )
                                if article_ids:
                                    return article_ids
                            except json.JSONDecodeError:
                                logger.error(
                                    f"Error parsing cached articles JSON for {query}"
                                )

                # If no cache or cache error, fetch from PubMed API
                logger.debug(f"Querying pubmed for query {query}")
                pmids = await sync_to_async(pubmed_fetcher.pmids_for_query)(
                    query, since=since
                )
                logger.debug(f"Got back initial pmids {pmids}")
                if pmids and len(pmids) > 0:
                    # Sometimes we get nulls...
                    articles_json = json.dumps(pmids).replace("\x00", "")
                    await PubMedQueryData.objects.acreate(
                        query=query,
                        since=since,
                        articles=articles_json,
                    )
                    return pmids
        except asyncio.TimeoutError as e:
            # We might timeout
            logger.opt(exception=True).debug(
                f"Error or timeout in find_pubmed_article_ids_for_query: {e}"
            )
            pass
        except asyncio.exceptions.CancelledError:
            # We might be cancelled
            logger.opt(exception=True).debug(
                f"Cancelled in find_pubmed_article_ids_for_query: {query}"
            )
            pass
        return pmids

    async def fetch_mesh_and_pub_types(
        self,
        pmids: List[str],
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = 15.0,
        batch_size: int = _EFETCH_BATCH_SIZE,
    ) -> Dict[str, Dict[str, List[str]]]:
        """Fetch MeSH terms and publication types for a list of PMIDs via efetch.

        Returns ``{pmid: {"mesh": [...], "pub_types": [...]}}``. Missing PMIDs
        are simply absent from the result rather than raising. Network errors
        and XML parse failures degrade to a partial (or empty) result.
        ``CancelledError`` is re-raised so request cancellation propagates;
        ``TimeoutError`` returns whatever was collected so far.

        Splits ``pmids`` into ``batch_size`` chunks to avoid URL-length caps
        and NCBI's recommended single-request limit. ``timeout`` applies to
        the whole batched operation.
        """
        if not pmids:
            return {}

        result: Dict[str, Dict[str, List[str]]] = {}
        url = f"{_EUTILS_BASE}/efetch.fcgi"
        try:
            async with _ensure_session(session) as s:
                async with async_timeout(timeout):
                    for i in range(0, len(pmids), batch_size):
                        batch = pmids[i : i + batch_size]
                        params = {
                            **_EUTILS_PARAMS,
                            "db": "pubmed",
                            "id": ",".join(batch),
                            "rettype": "xml",
                            "retmode": "xml",
                        }
                        async with s.get(url, params=params) as resp:
                            if resp.status != 200:
                                logger.debug(
                                    f"efetch returned {resp.status} for batch starting at {i}"
                                )
                                continue
                            body = await resp.text()
                        self._merge_efetch_xml_into(body, result)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            logger.debug(f"Timeout in fetch_mesh_and_pub_types: {e}")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error in fetch_mesh_and_pub_types: {e}")
        return result

    @staticmethod
    def _merge_efetch_xml_into(
        body: str, result: Dict[str, Dict[str, List[str]]]
    ) -> None:
        """Parse an efetch XML body and merge ``{pmid: {mesh, pub_types}}`` rows
        into ``result``. Silently skips a malformed body so one bad batch
        doesn't poison the rest."""
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            logger.debug(f"Failed to parse efetch XML: {e}")
            return
        for art in root.findall(".//PubmedArticle"):
            pmid_el = art.find(".//PMID")
            if pmid_el is None or not pmid_el.text:
                continue
            pmid = pmid_el.text.strip()
            mesh_terms = [
                descriptor.text.strip()
                for descriptor in art.findall(".//MeshHeading/DescriptorName")
                if descriptor.text
            ]
            pub_types = [
                pt.text.strip()
                for pt in art.findall(".//PublicationTypeList/PublicationType")
                if pt.text
            ]
            result[pmid] = {"mesh": mesh_terms, "pub_types": pub_types}

    async def find_related_pmids(
        self,
        pmid: str,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = 15.0,
        max_results: int = 10,
    ) -> List[str]:
        """Return PMIDs related to ``pmid`` via NCBI's elink ``pubmed_pubmed`` set.

        Useful for query expansion when a user's free-text terms are too
        narrow or use lay vocabulary. Returns at most ``max_results`` PMIDs
        and excludes the input PMID itself.
        """
        if not pmid:
            return []

        related: List[str] = []
        params = {
            **_EUTILS_PARAMS,
            "dbfrom": "pubmed",
            "db": "pubmed",
            "id": pmid,
            "linkname": "pubmed_pubmed",
            "retmode": "json",
        }
        url = f"{_EUTILS_BASE}/elink.fcgi"
        try:
            async with _ensure_session(session) as s:
                async with async_timeout(timeout):
                    async with s.get(url, params=params) as resp:
                        if resp.status != 200:
                            return []
                        data = await resp.json()
            for linkset in data.get("linksets", []):
                for linksetdb in linkset.get("linksetdbs", []):
                    if linksetdb.get("linkname") != "pubmed_pubmed":
                        continue
                    for related_id in linksetdb.get("links", []):
                        related_id_str = str(related_id)
                        if related_id_str != pmid and related_id_str not in related:
                            related.append(related_id_str)
                        if len(related) >= max_results:
                            return related
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            logger.debug(f"Timeout in find_related_pmids({pmid}): {e}")
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error in find_related_pmids({pmid}): {e}"
            )
        return related

    async def structured_search(
        self,
        condition: Optional[str] = None,
        treatment: Optional[str] = None,
        pub_type_preset: Optional[str] = None,
        pub_type_filters: Optional[List[str]] = None,
        mesh_terms: Optional[List[str]] = None,
        since_year: Optional[str] = None,
        until_year: Optional[str] = None,
        extra_terms: Optional[List[str]] = None,
        max_results: int = 20,
        expand_with_related: bool = False,
        timeout: float = 60.0,
    ) -> Dict[EvidenceStrength, List[PubMedMiniArticle]]:
        """Run a structured PubMed search and return articles bucketed by strength.

        Builds a query from ``condition``/``treatment`` plus optional
        publication-type filters, MeSH constraints, and a date range; runs
        the search; fetches publication types in a single batched ``efetch``;
        and categorizes the result into strong / moderate / weak / unknown
        evidence tiers.

        Args:
            condition: Diagnosis or condition (e.g., "rheumatoid arthritis").
            treatment: Procedure or medication (e.g., "infliximab").
            pub_type_preset: Preset key from ``PUB_TYPE_PRESETS``
                (e.g., "guidelines_and_systematic_reviews").
            pub_type_filters: Explicit filter keys from ``PUB_TYPE_FILTERS``.
            mesh_terms: MeSH descriptors to require (joined with AND).
            since_year: Earliest publication year (defaults to no lower bound).
            until_year: Latest publication year (defaults to current year
                if ``since_year`` is set).
            extra_terms: Additional free-text terms (e.g., microsite
                keywords).
            max_results: Cap on total PMIDs to materialize.
            expand_with_related: When true and the initial search returns at
                least one PMID, fetch related PMIDs via elink and add them to
                the result set (useful when user vocabulary is too narrow).
            timeout: Wall-clock timeout for the whole operation.

        Returns:
            A dict keyed by ``EvidenceStrength`` with ``PubMedMiniArticle``
            lists in each bucket. All four tiers are always present.
        """
        empty_buckets: Dict[EvidenceStrength, List[PubMedMiniArticle]] = {
            s: [] for s in EvidenceStrength
        }
        structured = build_structured_query(
            condition=condition,
            treatment=treatment,
            pub_type_preset=pub_type_preset,
            pub_type_filters=pub_type_filters,
            mesh_terms=mesh_terms,
            since_year=since_year,
            until_year=until_year,
            extra_terms=extra_terms,
        )
        if not structured.query:
            return empty_buckets

        try:
            async with async_timeout(timeout):
                # since_year is already encoded into the date filter inside
                # the query string, so pass since=None here to avoid
                # double-filtering with metapub's own ``since`` keyword.
                pmids = await self.find_pubmed_article_ids_for_query(
                    structured.query,
                    since=None,
                    timeout=timeout / 2,
                )
                pmids = [p for p in pmids if p][:max_results]

                if expand_with_related and pmids:
                    seed = pmids[0]
                    related = await self.find_related_pmids(
                        seed,
                        timeout=timeout / 4,
                        max_results=max_results,
                    )
                    for related_pmid in related:
                        if related_pmid not in pmids and len(pmids) < max_results:
                            pmids.append(related_pmid)

                if not pmids:
                    return empty_buckets

                articles = await self._materialize_mini_articles(
                    pmids, per_source_timeout=timeout / 12
                )
                pub_types_by_pmid = await self.fetch_mesh_and_pub_types(
                    [a.pmid for a in articles],
                    timeout=timeout / 4,
                )
                pub_types_lookup = {
                    pmid: data.get("pub_types", [])
                    for pmid, data in pub_types_by_pmid.items()
                }
                return categorize_articles_by_strength(
                    articles, pub_types_lookup=pub_types_lookup
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.debug(f"Timeout in structured_search: {structured.query}")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error in structured_search: {e}")
        return empty_buckets

    async def _materialize_mini_articles(
        self,
        pmids: List[str],
        per_source_timeout: float = 5.0,
    ) -> List[PubMedMiniArticle]:
        """Resolve a PMID list into ``PubMedMiniArticle`` rows, fetching as needed.

        Reuses cached rows when present; otherwise fetches metadata via
        metapub and creates a new row. Skips PMIDs that fail to fetch.
        """
        articles: List[PubMedMiniArticle] = []
        async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
            for pmid in pmids:
                cached = await PubMedMiniArticle.objects.filter(pmid=pmid).afirst()
                if cached:
                    articles.append(cached)
                    continue
                try:
                    fetched = await sync_to_async(pubmed_fetcher.article_by_pmid)(pmid)
                    if not fetched:
                        continue
                    doi = getattr(fetched, "doi", None)
                    url = None
                    try:
                        url = await asyncio.wait_for(
                            self._find_article_url(
                                pmid,
                                doi=doi,
                                session=session,
                                per_source_timeout=per_source_timeout,
                            ),
                            timeout=per_source_timeout * 2,
                        )
                    except Exception as e:
                        logger.debug(f"URL discovery failed for {pmid}: {e}")
                    title = (fetched.title or "").replace("\x00", "")
                    abstract = (fetched.abstract or "").replace("\x00", "")
                    article = await PubMedMiniArticle.objects.acreate(
                        pmid=pmid,
                        title=title,
                        abstract=abstract,
                        article_url=url,
                    )
                    articles.append(article)
                except Exception as e:
                    logger.debug(f"Failed to materialize {pmid}: {e}")
        return articles

    async def find_pubmed_articles_for_denial(
        self, denial: Denial, timeout=70.0
    ) -> List[PubMedMiniArticle]:
        """
        Asynchronously retrieves and returns a list of PubMedMiniArticle objects relevant to a medical denial.

        Constructs a PubMed search query from the denial's procedure and diagnosis, searches for recent articles across multiple years, and limits the number of articles per query. For each unique PubMed ID found, attempts to retrieve a cached article from the database or fetches metadata from PubMed and stores it if not present. Handles timeouts and logs errors, returning all successfully retrieved articles.

        If the denial has a microsite_slug, also uses the microsite's pubmed_search_terms.
        """
        pmids: List[str] = []
        articles: List[PubMedMiniArticle] = []
        logger.debug(f"Looking up pubmed articles...")

        # Re-fetch denial to ensure procedure/diagnosis/microsite_slug are current,
        # since this method may run in a fire-and-forget after the caller has moved on.
        denial = await Denial.objects.aget(pk=denial.denial_id)

        procedure_opt = denial.procedure if denial.procedure else ""
        diagnosis_opt = denial.diagnosis if denial.diagnosis else ""
        query = f"{procedure_opt} {diagnosis_opt}".strip()
        # Build an ordered, deduplicated list of queries: base query first,
        # then microsite search terms. Order matters for deterministic PER_QUERY
        # truncation when queries share overlapping PubMed results.
        queries: List[str] = []
        seen_queries: Set[str] = set()
        if query:
            queries.append(query)
            seen_queries.add(query)
        if denial.microsite_slug:
            try:
                microsite = get_microsite(denial.microsite_slug)
                if microsite and microsite.pubmed_search_terms:
                    for term in microsite.pubmed_search_terms:
                        stripped = term.strip()
                        if stripped and stripped not in seen_queries:
                            queries.append(stripped)
                            seen_queries.add(stripped)
                    if microsite.pubmed_search_terms:
                        logger.debug(
                            f"Adding {len(microsite.pubmed_search_terms)} microsite search terms for {denial.microsite_slug}"
                        )
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Failed to load microsite search terms: {e}"
                )

        try:
            async with async_timeout(timeout):
                # Allow us to remove duplicates while preserving order
                unique_pmids: Set[str] = set()

                for since in self.since_list:
                    for q in queries:
                        count = 0
                        if q is None or q.strip() == "":
                            continue
                        new_pmids = await self.find_pubmed_article_ids_for_query(
                            q, since=since
                        )
                        for pmid in new_pmids:
                            if pmid not in unique_pmids:
                                count = count + 1
                                unique_pmids.add(pmid)
                                pmids.append(pmid)
                                if count >= PER_QUERY:
                                    break

                # Check if articles already exist in database
                url_timeout = timeout / 5.0
                per_source = url_timeout / 6
                async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
                    for pmid in pmids:
                        mini_article = await PubMedMiniArticle.objects.filter(
                            pmid=pmid
                        ).afirst()
                        if mini_article:
                            if not mini_article.article_url:
                                try:
                                    fetched = await sync_to_async(
                                        pubmed_fetcher.article_by_pmid
                                    )(pmid)
                                    doi = (
                                        getattr(fetched, "doi", None)
                                        if fetched
                                        else None
                                    )
                                    url = await asyncio.wait_for(
                                        self._find_article_url(
                                            pmid,
                                            doi=doi,
                                            session=session,
                                            per_source_timeout=per_source,
                                        ),
                                        timeout=url_timeout,
                                    )
                                    if url:
                                        await PubMedMiniArticle.objects.filter(
                                            pmid=pmid
                                        ).aupdate(article_url=url)
                                        mini_article.article_url = url
                                except Exception as e:
                                    logger.debug(
                                        f"URL re-resolution failed for cached {pmid}: {e}"
                                    )
                            articles.append(mini_article)
                        else:
                            # Create a new mini article
                            try:
                                fetched = await sync_to_async(
                                    pubmed_fetcher.article_by_pmid
                                )(pmid)
                                if fetched:
                                    doi = getattr(fetched, "doi", None)
                                    url = None
                                    try:
                                        url = await asyncio.wait_for(
                                            self._find_article_url(
                                                pmid,
                                                doi=doi,
                                                session=session,
                                                per_source_timeout=per_source,
                                            ),
                                            timeout=url_timeout,
                                        )
                                    except Exception as e:
                                        logger.debug(
                                            f"URL discovery failed for {pmid}: {e}"
                                        )
                                    # Always create the article, even without a URL
                                    mini_article = (
                                        await PubMedMiniArticle.objects.acreate(
                                            pmid=pmid,
                                            title=(
                                                fetched.title.replace("\x00", "")
                                                if fetched.title
                                                else ""
                                            ),
                                            abstract=(
                                                fetched.abstract.replace("\x00", "")
                                                if fetched.abstract
                                                else ""
                                            ),
                                            article_url=url,
                                        )
                                    )
                                    articles.append(mini_article)
                            except Exception as e:
                                logger.opt(exception=True).debug(
                                    f"Error fetching article {pmid}: {e}"
                                )
        except asyncio.TimeoutError as e:
            logger.debug(
                f"Timeout in find_pubmed_articles_for_denial: {e} so far got {articles}"
            )
        except asyncio.exceptions.CancelledError:
            logger.opt(exception=True).debug(
                f"Cancelled in find_pubmed_articles_for_denial; so far got {articles}"
            )
            pass
        except Exception as e:
            logger.opt(exception=True).debug(f"Unexpected error {e}")
            raise e
        # queries is already deduplicated and stripped; sort for stable summary string
        valid_queries = sorted(queries)
        if valid_queries and pmids:
            articles_json = json.dumps(pmids)
            all_queries_str = " | ".join(valid_queries)
            await PubMedQueryData.objects.acreate(
                denial_id=denial,
                articles=articles_json,
                query=all_queries_str,
            )
        logger.debug(f"Found {articles} for denial {denial}")
        return articles

    async def find_context_for_denial(self, denial: Denial, timeout=70.0) -> str:
        result = await self._find_context_for_denial(denial, timeout)
        if result is not None and len(result) > 1:
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                pubmed_context=result
            )
        return result

    async def _find_context_for_denial(self, denial: Denial, timeout=70.0) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
        if denial.pubmed_context and len(denial.pubmed_context) > 1:
            return denial.pubmed_context
        procedure_opt = denial.procedure if denial.procedure else ""
        diagnosis_opt = denial.diagnosis if denial.diagnosis else ""
        query = f"{procedure_opt} {diagnosis_opt}".strip()
        articles: list[PubMedArticleSummarized] = []
        missing_pmids: list[str] = []

        try:
            async with async_timeout(timeout):
                # Check if the denial has specific pubmed IDs selected already
                selected_pmids: Optional[list[str]] = None
                if denial.pubmed_ids_json and len(denial.pubmed_ids_json) > 0:
                    try:
                        selected_pmids = denial.pubmed_ids_json
                        if selected_pmids:  # Check if not None
                            logger.info(
                                f"Using {len(selected_pmids)} pre-selected PubMed articles for denial {denial.denial_id}"
                            )
                    except json.JSONDecodeError:
                        logger.error(
                            f"Error parsing pubmed_ids_json for denial {denial.denial_id}"
                        )
                # If we still don't have any articles (no selected PMIDs or couldn't find them), search for some
                if not selected_pmids or len(selected_pmids) == 0:
                    logger.debug(
                        f"No pre-selected articles found, searching for PubMed articles"
                    )
                    if not query or query.strip() == "":
                        return ""  # Return empty string if no query available

                    possible_articles = await asyncio.wait_for(
                        self.find_pubmed_articles_for_denial(
                            denial, timeout=(timeout / 2.5)
                        ),
                        timeout=timeout / 1.5,
                    )

                    selected_pmids = list(map(lambda x: x.pmid, possible_articles))

                logger.debug(f"Updating denial to have some context selected...")
                # Use aupdate instead of asave to avoid race conditions
                await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                    pubmed_ids_json=selected_pmids
                )
                # Directly fetch the selected articles from the database
                articles = [
                    article
                    async for article in PubMedArticleSummarized.objects.filter(
                        pmid__in=selected_pmids
                    )
                ]
                logger.debug(
                    f"Found {len(articles)} pre-selected articles in the database"
                )

                # If we couldn't find all the articles in the database, try to fetch them
                if articles and len(articles) < len(selected_pmids):
                    for pmid in selected_pmids:
                        if not any(a.pmid == pmid for a in articles):
                            missing_pmids.append(pmid)
                elif articles and len(articles) == len(selected_pmids):
                    missing_pmids = []
                else:
                    missing_pmids = selected_pmids

                if missing_pmids and len(missing_pmids) > 0:
                    logger.debug(f"Fetching {len(missing_pmids)} missing articles")
                    # Fetch in-order so we can be interrupted
                    for pmid in missing_pmids:
                        articles.extend(await self.get_articles([pmid]))
        except asyncio.TimeoutError as e:
            # Use aupdate instead of asave to avoid race conditions
            logger.debug(f"Timed out saving articles so far.")
            logger.debug(
                f"Timeout in find_context_for_denial: {e} so far got {articles}"
            )
        except asyncio.exceptions.CancelledError:
            logger.opt(exception=True).debug(
                f"Cancelled in find_context_for_denial; so far got {articles}"
            )
            pass
        except Exception as e:
            logger.opt(exception=True).debug(f"Non-timeout error -- {e}?")
            raise
        finally:
            if selected_pmids:
                logger.debug(f"Writing back selected pmids {selected_pmids}")
                await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                    pubmed_ids_json=selected_pmids
                )

        # Format the articles for context
        if articles:
            logger.debug("Making single context input")
            joined_contexts = "\n".join(
                self.format_article_short(article) for article in articles
            )
            if len(joined_contexts) < 100:
                logger.debug("Not much input, skpping summary step.")
                return joined_contexts
            r: Optional[str] = await ml_router.summarize(
                title="Combined contexts", text=joined_contexts
            )
            logger.debug("Summarized combined contexts")
            if r is None:
                return joined_contexts
            else:
                return r
        else:
            return ""

    @staticmethod
    def format_article_short(article: PubMedArticleSummarized) -> str:
        """Helper function to format an article for context with full citation metadata."""
        summary = None
        if article.basic_summary:
            summary = article.basic_summary
        elif article.abstract:
            summary = article.abstract[0:500]

        parts = []
        if article.authors:
            parts.append(f"Authors: {article.authors}")
        if article.title:
            parts.append(f"Title: {article.title}")
        if article.journal:
            parts.append(f"Journal: {article.journal}")
        if article.year:
            parts.append(f"Year: {article.year}")
        if article.doi:
            parts.append(f"DOI: {article.doi}")
        if article.pmid:
            parts.append(f"PMID: {article.pmid}")
        if article.article_url:
            parts.append(f"URL: {article.article_url}")
        if summary:
            parts.append(f"Summary: {summary}")

        return "; ".join(parts)

    async def get_articles(
        self, pubmed_ids: List[str]
    ) -> List[PubMedArticleSummarized]:
        """Get PubMed articles by their IDs."""
        if not pubmed_ids:
            return []

        pubmed_docs: List[PubMedArticleSummarized] = []
        for pmid in pubmed_ids:
            if pmid is None or pmid == "":
                continue

            try:
                # Look for existing articles in the database first
                matching_articles = PubMedArticleSummarized.objects.filter(pmid=pmid)
                if await matching_articles.aexists():
                    article = await matching_articles.afirst()
                    if article is not None:
                        pubmed_docs.append(article)
                else:
                    # Article not in database, fetch it
                    article = await self.do_article_summary(pmid)
                    if article is not None:
                        pubmed_docs.append(article)
            except Exception as e:
                logger.debug(f"Skipping {pmid}: {e}")

        return pubmed_docs

    async def _fetch_text_from_url(
        self,
        url: str,
        session: aiohttp.ClientSession,
        timeout_secs: float = 15.0,
    ) -> str:
        """Fetch article text from a URL, handling both PDF and HTML content."""
        article_text = ""
        try:
            async with async_timeout(timeout_secs):
                async with session.get(url, headers=_FETCH_HEADERS) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "")
                    if self._is_pdf_response(url, content_type):
                        pdf_bytes = await response.read()
                        read_pdf = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                        if read_pdf.is_encrypted:
                            read_pdf.decrypt("")
                        for page in read_pdf.pages:
                            article_text += page.extract_text()
                    else:
                        text = (await response.text()).strip()
                        if " " in text and len(text) > 50:
                            article_text = text
        except Exception as e:
            logger.debug(f"Error fetching text from {url}: {e}")
        return article_text

    async def do_article_summary(self, article_id) -> Optional[PubMedArticleSummarized]:
        article: Optional[PubMedArticleSummarized] = (
            await PubMedArticleSummarized.objects.filter(
                pmid=article_id,
                basic_summary__isnull=False,
            ).afirst()
        )

        if article is None:
            try:
                fetched = pubmed_fetcher.article_by_pmid(article_id)
                article_text = ""
                url = None
                doi = getattr(fetched, "doi", None) if fetched else None

                async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
                    url = await self._find_article_url(
                        article_id, doi=doi, session=session
                    )

                    if url is not None:
                        article_text = await self._fetch_text_from_url(url, session)

                if (
                    (article_text is None or article_text == "")
                    and fetched
                    and hasattr(fetched, "content")
                    and hasattr(fetched.content, "text")
                ):
                    article_text = fetched.content.text

                title = (getattr(fetched, "title", "") or "").strip()
                abstract = (getattr(fetched, "abstract", "") or "").strip()
                if article_text:
                    article_text = article_text.strip()
                authors = getattr(fetched, "authors", None)
                authors_str = ", ".join(authors) if authors else ""
                journal_str = getattr(fetched, "journal", "") or ""
                year_val = getattr(fetched, "year", None)
                year_str = str(year_val) if year_val else ""

                if fetched is not None and (
                    (abstract and len(abstract) > 40)
                    or (article_text and len(article_text) > 40)
                ):
                    article = await PubMedArticleSummarized.objects.acreate(
                        pmid=article_id,
                        doi=getattr(fetched, "doi", "") or "",
                        title=title,
                        authors=authors_str,
                        journal=journal_str,
                        year=year_str,
                        abstract=abstract,
                        text=article_text,
                        article_url=url,
                        basic_summary=await ml_router.summarize(
                            title=title,
                            abstract=abstract,
                            text=article_text,
                        ),
                    )
                    return article
            except Exception as e:
                logger.debug(f"Error in do_article_summary for {article_id}: {e}")
                return None

        return article

    async def _try_fetch_pdf_to_file(
        self,
        url: str,
        prefix: str,
        session: aiohttp.ClientSession,
    ) -> Optional[str]:
        """Try to fetch a PDF from a URL and save to a temp file. Returns path or None."""
        try:
            async with session.get(url, headers=_FETCH_HEADERS) as response:
                if response.status == 200:
                    content_type = response.headers.get("Content-Type", "")
                    if self._is_pdf_response(url, content_type):
                        content = await response.read()
                        if len(content) > 512 and content.lstrip().startswith(b"%PDF"):
                            with tempfile.NamedTemporaryFile(
                                prefix=prefix, suffix=".pdf", delete=False
                            ) as my_data:
                                my_data.write(content)
                                my_data.flush()
                                return my_data.name
        except Exception as e:
            logger.debug(f"Error fetching PDF from {url}: {e}")
        return None

    async def article_as_pdf(self, article: PubMedArticleSummarized) -> Optional[str]:
        """Return the best PDF we can find of the article."""
        article_id = article.pmid

        try:
            async with async_timeout(30.0):
                async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
                    # Try the stored URL first
                    if article.article_url:
                        result = await self._try_fetch_pdf_to_file(
                            article.article_url, prefix=f"{article_id}", session=session
                        )
                        if result:
                            return result
                        logger.debug(
                            f"Stored URL {article.article_url} didn't yield PDF, trying other sources"
                        )

                    # Iterate sources individually so non-PDF results don't block fallbacks
                    for name, make_coro in self._url_sources(
                        article_id, article.doi, session, 5.0
                    ):
                        url = await make_coro()
                        if not url:
                            continue
                        result = await self._try_fetch_pdf_to_file(
                            url, prefix=f"{article_id}", session=session
                        )
                        if result:
                            if url != article.article_url:
                                try:
                                    await PubMedArticleSummarized.objects.filter(
                                        pmid=article_id
                                    ).aupdate(article_url=url)
                                except Exception as e:
                                    logger.debug(
                                        f"[{article_id}] Failed to update article_url to {url}: {e}"
                                    )
                            return result
                        logger.debug(
                            f"[{article_id}] {name} URL {url} didn't yield PDF, continuing"
                        )
        except Exception as e:
            logger.debug(f"Error {e} fetching article PDF for {article}")

        # Backup: markdown & pandoc -- but only if we have something to write
        if article.abstract is None and article.text is None:
            return None

        markdown_text = f"# {markdown_escape(article.title)} \n\n PMID {article.pmid} / DOI {article.doi}\n\n{markdown_escape(article.abstract)}\n\n---{markdown_escape(article.text)}"
        with tempfile.NamedTemporaryFile(
            prefix=f"{article.pmid}",
            suffix=".md",
            delete=False,
            encoding="utf-8",
            mode="w",
        ) as my_data:
            my_data.write(markdown_text)
            my_data.flush()
            command = [
                "pandoc",
                "--read=markdown",
                "--wrap=auto",
                my_data.name,
                f"-o{my_data.name}.pdf",
            ]
            try:
                await _try_pandoc_engines(command)
                return f"{my_data.name}.pdf"
            except Exception as e:
                logger.debug(f"Error processing {command}: {e}")
        return None

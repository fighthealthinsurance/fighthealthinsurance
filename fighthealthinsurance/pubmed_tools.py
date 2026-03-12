import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urljoin

import aiohttp
import eutils
import PyPDF2
import requests
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
from fighthealthinsurance.utils import pubmed_fetcher

from .exec import pubmed_executor
from .models import Denial
from .utils import _try_pandoc_engines, markdown_escape

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


PER_QUERY = 2

# Common headers to avoid bot detection when fetching PDFs
_FETCH_HEADERS = {
    "User-Agent": "FightHealthInsurance/1.0 (mailto:support@fighthealthinsurance.com)",
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*",
}


class PubMedTools(object):
    # Rough bias to "recent" articles
    since_list = ["2024", None]

    async def _find_article_url(
        self,
        pmid: str,
        doi: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        per_source_timeout: float = 10.0,
    ) -> Optional[str]:
        """Aggressively try multiple sources to find a PDF/full-text URL for an article.

        Tries in order:
        1. metapub FindIt (existing approach)
        2. PubMed Central (PMC) direct PDF link
        3. Europe PMC full text
        4. Unpaywall API (open access finder)
        5. medRxiv/bioRxiv preprint PDF
        6. DOI resolution to publisher with PDF link detection
        """
        url: Optional[str] = None
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession(headers=_FETCH_HEADERS)
        assert session is not None

        try:
            # 1. metapub FindIt
            url = await self._try_findit(pmid, per_source_timeout)
            if url:
                logger.debug(f"[{pmid}] Found URL via FindIt: {url}")
                return url

            # 2. PMC direct PDF
            url = await self._try_pmc(pmid, session, per_source_timeout)
            if url:
                logger.debug(f"[{pmid}] Found URL via PMC: {url}")
                return url

            # 3. Europe PMC
            url = await self._try_europe_pmc(pmid, session, per_source_timeout)
            if url:
                logger.debug(f"[{pmid}] Found URL via Europe PMC: {url}")
                return url

            # 4. Unpaywall (needs DOI)
            if doi:
                url = await self._try_unpaywall(doi, session, per_source_timeout)
                if url:
                    logger.debug(f"[{pmid}] Found URL via Unpaywall: {url}")
                    return url

            # 5. medRxiv/bioRxiv (needs DOI)
            if doi:
                url = await self._try_preprint_servers(doi, session, per_source_timeout)
                if url:
                    logger.debug(f"[{pmid}] Found URL via preprint server: {url}")
                    return url

            # 6. DOI resolution
            if doi:
                url = await self._try_doi_resolution(doi, session, per_source_timeout)
                if url:
                    logger.debug(f"[{pmid}] Found URL via DOI resolution: {url}")
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

    async def _try_pmc(
        self,
        pmid: str,
        session: aiohttp.ClientSession,
        timeout_secs: float,
    ) -> Optional[str]:
        """Check if article is in PubMed Central and get its PDF URL."""
        try:
            # Use NCBI ID converter to find PMC ID
            converter_url = (
                f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
                f"?ids={pmid}&format=json&tool=fighthealthinsurance&email=support@fighthealthinsurance.com"
            )
            async with async_timeout(timeout_secs):
                async with session.get(converter_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        records = data.get("records", [])
                        if records and records[0].get("pmcid"):
                            pmcid = records[0]["pmcid"]
                            pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
                            # Verify it's accessible
                            async with session.head(
                                pdf_url, allow_redirects=True
                            ) as head_resp:
                                if head_resp.status == 200:
                                    return pdf_url
        except Exception as e:
            logger.debug(f"[{pmid}] PMC lookup failed: {e}")
        return None

    async def _try_europe_pmc(
        self,
        pmid: str,
        session: aiohttp.ClientSession,
        timeout_secs: float,
    ) -> Optional[str]:
        """Try Europe PMC for full text PDF."""
        try:
            api_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:{pmid}%20AND%20SRC:MED&resultType=core&format=json"
            async with async_timeout(timeout_secs):
                async with session.get(api_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("resultList", {}).get("result", [])
                        if results:
                            result = results[0]
                            # Check for full text URLs
                            full_text_urls = result.get("fullTextUrlList", {}).get(
                                "fullTextUrl", []
                            )
                            # Prefer PDF, then HTML
                            for ft_url in full_text_urls:
                                if ft_url.get("documentStyle") == "pdf" and ft_url.get(
                                    "availabilityCode"
                                ) in ("OA", "F"):
                                    return str(ft_url.get("url"))
                            for ft_url in full_text_urls:
                                if ft_url.get("availabilityCode") in (
                                    "OA",
                                    "F",
                                ) and ft_url.get("url"):
                                    return str(ft_url.get("url"))
        except Exception as e:
            logger.debug(f"[{pmid}] Europe PMC lookup failed: {e}")
        return None

    async def _try_unpaywall(
        self,
        doi: str,
        session: aiohttp.ClientSession,
        timeout_secs: float,
    ) -> Optional[str]:
        """Try Unpaywall API to find open access PDF."""
        try:
            encoded_doi = quote(doi, safe="")
            api_url = f"https://api.unpaywall.org/v2/{encoded_doi}?email=support@fighthealthinsurance.com"
            async with async_timeout(timeout_secs):
                async with session.get(api_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        best_oa = data.get("best_oa_location")
                        if best_oa:
                            # Prefer PDF URL, fall back to landing page
                            pdf_url = best_oa.get("url_for_pdf")
                            if pdf_url:
                                return str(pdf_url)
                            landing_url = best_oa.get("url_for_landing_page")
                            if landing_url:
                                return str(landing_url)
                        # Try all OA locations
                        for oa_loc in data.get("oa_locations", []):
                            pdf_url = oa_loc.get("url_for_pdf")
                            if pdf_url:
                                return str(pdf_url)
        except Exception as e:
            logger.debug(f"[DOI:{doi}] Unpaywall lookup failed: {e}")
        return None

    async def _try_preprint_servers(
        self,
        doi: str,
        session: aiohttp.ClientSession,
        timeout_secs: float,
    ) -> Optional[str]:
        """Try medRxiv and bioRxiv for preprint PDFs."""
        doi_lower = doi.lower()

        # Direct medRxiv/bioRxiv DOI pattern
        for server in ("medrxiv", "biorxiv"):
            if server in doi_lower or "10.1101/" in doi:
                try:
                    # medRxiv/bioRxiv API
                    api_url = f"https://api.biorxiv.org/details/{server}/{doi}"
                    async with async_timeout(timeout_secs):
                        async with session.get(api_url) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                results = data.get("collection", [])
                                if results:
                                    jatsxml = results[-1].get("jatsxml")
                                    if jatsxml:
                                        # Convert JATSXML URL to PDF URL
                                        pdf_url: str = str(jatsxml).replace(
                                            ".source.xml", ".full.pdf"
                                        )
                                        return pdf_url
                                    # Try constructing PDF URL from DOI
                                    biorxiv_doi = str(results[-1].get("doi", doi))
                                    return f"https://www.{server}.org/content/{biorxiv_doi}.full.pdf"
                except Exception as e:
                    logger.debug(f"[DOI:{doi}] {server} lookup failed: {e}")

        # Even if the DOI doesn't look like medRxiv/bioRxiv, search for the DOI
        # on these servers as they may host preprints of published papers
        for server in ("medrxiv", "biorxiv"):
            try:
                api_url = f"https://api.biorxiv.org/details/{server}/{doi}"
                async with async_timeout(timeout_secs / 2):
                    async with session.get(api_url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            results = data.get("collection", [])
                            if results:
                                biorxiv_doi = str(results[-1].get("doi", ""))
                                if biorxiv_doi:
                                    return f"https://www.{server}.org/content/{biorxiv_doi}.full.pdf"
            except Exception as e:
                logger.debug(f"[DOI:{doi}] {server} search failed: {e}")
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

                        # If the DOI resolved directly to a PDF
                        if "application/pdf" in content_type:
                            return final_url

                        # Check for common publisher PDF URL patterns
                        if "text/html" in content_type:
                            html = await resp.text()
                            pdf_url = self._extract_pdf_url_from_html(html, final_url)
                            if pdf_url:
                                return pdf_url
        except Exception as e:
            logger.debug(f"[DOI:{doi}] DOI resolution failed: {e}")
        return None

    @staticmethod
    def _extract_pdf_url_from_html(html: str, base_url: str) -> Optional[str]:
        """Extract PDF URL from publisher HTML page using common patterns."""
        # Common meta tag patterns for PDF links
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
                ).order_by("-created")

                if await existing_queries.aexists():
                    # Use cached query results
                    async for query_data in existing_queries:
                        if query_data.articles:
                            try:
                                article_ids: list[str] = json.loads(
                                    query_data.articles.replace("\x00", "")
                                )
                                # Return the cached IDs
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
        try:
            async with async_timeout(timeout):
                procedure_opt = denial.procedure if denial.procedure else ""
                diagnosis_opt = denial.diagnosis if denial.diagnosis else ""
                query = f"{procedure_opt} {diagnosis_opt}".strip()
                # Allow us to remove duplicates while preserving order
                unique_pmids: Set[str] = set()
                queries: Set[str] = {
                    query,
                }

                # Add microsite pubmed search terms if available
                if denial.microsite_slug:
                    try:
                        microsite = get_microsite(denial.microsite_slug)
                        if microsite and microsite.pubmed_search_terms:
                            if len(microsite.pubmed_search_terms) > 0:
                                logger.debug(
                                    f"Adding {len(microsite.pubmed_search_terms)} microsite search terms for {denial.microsite_slug}"
                                )
                                queries.update(microsite.pubmed_search_terms)
                    except Exception as e:
                        logger.opt(exception=True).warning(
                            f"Failed to load microsite search terms: {e}"
                        )

                for since in self.since_list:
                    for query in queries:
                        count = 0
                        if query is None or query.strip() == "":
                            continue
                        new_pmids = await self.find_pubmed_article_ids_for_query(
                            query, since=since
                        )
                        for pmid in new_pmids:
                            if pmid not in unique_pmids:
                                count = count + 1
                                unique_pmids.add(pmid)
                                pmids.append(pmid)
                                if count >= PER_QUERY:
                                    break

                # Check if articles already exist in database
                async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
                    for pmid in pmids:
                        mini_article = await PubMedMiniArticle.objects.filter(
                            pmid=pmid
                        ).afirst()
                        if mini_article:
                            articles.append(mini_article)
                        else:
                            # Create a new mini article
                            try:
                                fetched = await sync_to_async(
                                    pubmed_fetcher.article_by_pmid
                                )(pmid)
                                if fetched:
                                    doi = getattr(fetched, "doi", None)
                                    t = timeout / 5.0
                                    logger.debug(
                                        f"Looking for {pmid} with timeout of {t}"
                                    )
                                    url = await asyncio.wait_for(
                                        self._find_article_url(
                                            pmid,
                                            doi=doi,
                                            session=session,
                                            per_source_timeout=t / 6,
                                        ),
                                        timeout=t,
                                    )
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
        articles_json = json.dumps(list(map(lambda a: a.pmid, articles)))
        await PubMedQueryData.objects.acreate(
            denial_id=denial,
            articles=articles_json,
            query=f"{denial.procedure or ''} {denial.diagnosis or ''}".strip()
            or "denial_articles",
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
            logger.debug("Huzzah!")
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
    ) -> str:
        """Fetch article text from a URL, handling both PDF and HTML content."""
        article_text = ""
        try:
            async with session.get(url, headers=_FETCH_HEADERS) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if ".pdf" in url or "application/pdf" in content_type:
                    with tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False
                    ) as my_data:
                        my_data.write(await response.read())
                        with open(my_data.name, "rb") as open_pdf_file:
                            read_pdf = PyPDF2.PdfReader(open_pdf_file)
                            if read_pdf.is_encrypted:
                                read_pdf.decrypt("")
                            for page in read_pdf.pages:
                                article_text += page.extract_text()
                else:
                    # Assume maybe text-ish
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

                # Use aggressive multi-source URL finder
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

                title = (fetched.title if hasattr(fetched, "title") else "").strip()
                abstract = (
                    fetched.abstract if hasattr(fetched, "abstract") else ""
                ).strip()
                if article_text:
                    article_text = article_text.strip()

                # Extract authors, journal, and year from metapub
                authors_str = ""
                if hasattr(fetched, "authors") and fetched.authors:
                    authors_str = ", ".join(fetched.authors)
                journal_str = (
                    fetched.journal
                    if hasattr(fetched, "journal") and fetched.journal
                    else ""
                )
                year_str = (
                    str(fetched.year)
                    if hasattr(fetched, "year") and fetched.year
                    else ""
                )

                if fetched is not None and (
                    (
                        hasattr(fetched, "abstract")
                        and fetched.abstract
                        and len(fetched.abstract) > 40
                    )
                    or (article_text and len(article_text) > 40)
                ):
                    article = await PubMedArticleSummarized.objects.acreate(
                        pmid=article_id,
                        doi=fetched.doi if hasattr(fetched, "doi") else "",
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
                    if ".pdf" in url or "application/pdf" in content_type:
                        content = await response.read()
                        if len(content) > 20:
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

                    # Aggressively search for a PDF URL using all sources
                    url = await self._find_article_url(
                        article_id,
                        doi=article.doi,
                        session=session,
                        per_source_timeout=5.0,
                    )
                    if url:
                        result = await self._try_fetch_pdf_to_file(
                            url, prefix=f"{article_id}", session=session
                        )
                        if result:
                            # Update the stored URL for next time
                            if url != article.article_url:
                                try:
                                    await PubMedArticleSummarized.objects.filter(
                                        pmid=article_id
                                    ).aupdate(article_url=url)
                                except Exception:
                                    pass
                            return result
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

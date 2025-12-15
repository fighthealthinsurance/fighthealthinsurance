from fighthealthinsurance.models import (
    GoogleScholarArticleSummarized,
    GoogleScholarQueryData,
    GoogleScholarMiniArticle,
)
from asgiref.sync import sync_to_async
from fighthealthinsurance.microsites import get_microsite

import asyncio
from .models import Denial
import json
from fighthealthinsurance.ml.ml_router import ml_router
from typing import List, Optional, Dict, Any, Set
from loguru import logger
from datetime import datetime, timedelta
import sys
import hashlib
import re

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


PER_QUERY = 2


def google_scholar_fetcher_sync(query: str, since: Optional[str] = None, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Synchronous wrapper for Google Scholar scraping using scholarly package.
    
    Args:
        query: Search query string
        since: Optional year filter to filter articles by publication year
        max_results: Maximum number of results to return (default 10)
    
    Returns:
        List of article dictionaries with keys: title, bib (contains year, author, etc),
        pub_url (link), num_citations, eprint_url (pdf if available)
    """
    from scholarly import scholarly, ProxyGenerator
    
    try:
        # Setup free proxy to avoid blocking (optional but recommended)
        # Note: FreeProxies can be slow, consider using paid proxies in production
        try:
            pg = ProxyGenerator()
            pg.FreeProxies()
            scholarly.use_proxy(pg)
        except Exception as proxy_error:
            logger.warning(f"Could not set up proxy, continuing without: {proxy_error}")
        
        # Search for publications
        search_query = scholarly.search_pubs(query)
        
        results = []
        count = 0
        
        # Iterate through results
        for result in search_query:
            if count >= max_results:
                break
                
            # Extract relevant information
            article_data = {
                'title': result.get('bib', {}).get('title', ''),
                'title_link': result.get('pub_url', ''),
                'publication_info': '',  # Will build from bib
                'snippet': result.get('bib', {}).get('abstract', ''),
                'cited_by_link': result.get('citedby_url', ''),
                'cited_by_count': result.get('num_citations', 0),
                'pdf_file': result.get('eprint_url', ''),
            }
            
            # Build publication info from bib
            bib = result.get('bib', {})
            pub_info_parts = []
            if 'author' in bib:
                # author might be a string or list
                authors = bib['author']
                if isinstance(authors, list):
                    pub_info_parts.append(', '.join(authors[:3]))  # First 3 authors
                else:
                    pub_info_parts.append(authors)
            if 'venue' in bib:
                pub_info_parts.append(bib['venue'])
            if 'pub_year' in bib:
                pub_info_parts.append(str(bib['pub_year']))
                
            article_data['publication_info'] = ', '.join(pub_info_parts)
            
            # Filter by year if specified
            if since:
                try:
                    since_year = int(since)
                    pub_year = bib.get('pub_year')
                    if pub_year:
                        try:
                            if int(pub_year) < since_year:
                                continue  # Skip this article
                        except (ValueError, TypeError):
                            pass  # Include if we can't parse the year
                except (ValueError, AttributeError):
                    logger.warning(f"Could not filter by year {since}")
            
            results.append(article_data)
            count += 1
        
        return results
        
    except Exception as e:
        logger.opt(exception=True).error(f"Error scraping Google Scholar for query '{query}': {e}")
        return []


class GoogleScholarTools(object):
    # Rough bias to "recent" articles
    since_list = ["2024", None]

    def _generate_article_id(self, result: Dict[str, Any]) -> str:
        """
        Generate a unique ID for a Google Scholar article.
        Uses SHA-256 hash of title and link to create a stable identifier.
        """
        title = result.get('title', '')
        link = result.get('title_link', '')
        unique_str = f"{title}|{link}"
        return hashlib.sha256(unique_str.encode()).hexdigest()

    async def find_google_scholar_article_ids_for_query(
        self,
        query: str,
        since: Optional[str] = None,
        timeout: float = 60.0,
    ) -> List[str]:
        """
        Asynchronously retrieves Google Scholar article IDs matching a search query, optionally filtered by year.

        Checks for recent cached results in the database and returns them if available. If not cached or if parsing fails,
        scrapes Google Scholar, stores new results in the database, and returns the list of article IDs.
        Returns an empty list if a timeout or cancellation occurs.

        Args:
            query: The Google Scholar search query string.
            since: Optional year to filter articles published since this year.
            timeout: Maximum time in seconds to spend on the operation.

        Returns:
            A list of Google Scholar article IDs (generated hashes) matching the query.
        """
        article_ids: List[str] = []

        try:
            async with async_timeout(timeout):
                # Check if we already have query results from the last month
                month_ago = datetime.now() - timedelta(days=30)
                existing_queries = GoogleScholarQueryData.objects.filter(
                    query=query,
                    created__gte=month_ago,
                    since=since,
                ).order_by("-created")

                if await existing_queries.aexists():
                    # Use cached query results
                    async for query_data in existing_queries:
                        if query_data.articles:
                            try:
                                cached_ids: list[str] = json.loads(
                                    query_data.articles.replace("\x00", "")
                                )
                                # Return the cached IDs
                                return cached_ids
                            except json.JSONDecodeError:
                                logger.error(
                                    f"Error parsing cached articles JSON for {query}"
                                )

                # If no cache or cache error, scrape from Google Scholar
                logger.debug(f"Scraping Google Scholar for query {query}")
                results = await sync_to_async(google_scholar_fetcher_sync)(
                    query, since=since
                )
                logger.debug(f"Got back {len(results)} Google Scholar results")
                
                if results and len(results) > 0:
                    # Generate IDs for the results
                    article_ids = [self._generate_article_id(result) for result in results]
                    
                    # Store the results in mini articles for later retrieval
                    for result, article_id in zip(results, article_ids):
                        # Check if mini article already exists
                        existing = await GoogleScholarMiniArticle.objects.filter(
                            article_id=article_id
                        ).afirst()
                        
                        if not existing:
                            await GoogleScholarMiniArticle.objects.acreate(
                                article_id=article_id,
                                title=result.get('title', ''),
                                snippet=result.get('snippet', ''),
                                publication_info=result.get('publication_info', ''),
                                cited_by_count=result.get('cited_by_count', 0),
                                cited_by_link=result.get('cited_by_link', ''),
                                article_url=result.get('title_link', ''),
                                pdf_file=result.get('pdf_file', ''),
                            )
                    
                    # Sometimes we get nulls...
                    articles_json = json.dumps(article_ids).replace("\x00", "")
                    await GoogleScholarQueryData.objects.acreate(
                        query=query,
                        since=since,
                        articles=articles_json,
                    )
                    return article_ids
        except asyncio.TimeoutError as e:
            # We might timeout
            logger.opt(exception=True).debug(
                f"Error or timeout in find_google_scholar_article_ids_for_query: {e}"
            )
            pass
        except asyncio.exceptions.CancelledError:
            # We might be cancelled
            logger.opt(exception=True).debug(
                f"Cancelled in find_google_scholar_article_ids_for_query: {query}"
            )
            pass
        return article_ids

    async def find_google_scholar_articles_for_denial(
        self, denial: Denial, timeout=70.0
    ) -> List[GoogleScholarMiniArticle]:
        """
        Asynchronously retrieves and returns a list of GoogleScholarMiniArticle objects relevant to a medical denial.

        Constructs a Google Scholar search query from the denial's procedure and diagnosis, searches for recent articles
        across multiple years, and limits the number of articles per query. For each unique article ID found, attempts to
        retrieve a cached article from the database or scrapes from Google Scholar and stores it if not present.
        Handles timeouts and logs errors, returning all successfully retrieved articles.

        If the denial has a microsite_slug, also uses the microsite's pubmed_search_terms for Google Scholar queries.
        """
        article_ids: List[str] = []
        articles: List[GoogleScholarMiniArticle] = []
        logger.debug(f"Looking up Google Scholar articles...")
        try:
            async with async_timeout(timeout):
                procedure_opt = denial.procedure if denial.procedure else ""
                diagnosis_opt = denial.diagnosis if denial.diagnosis else ""
                query = f"{procedure_opt} {diagnosis_opt}".strip()
                # Allow us to remove duplicates while preserving order
                unique_article_ids: Set[str] = set()
                queries: Set[str] = {
                    query,
                }

                # Add microsite pubmed search terms if available
                # We use PubMed search terms for Google Scholar as well
                if denial.microsite_slug:
                    try:
                        microsite = get_microsite(denial.microsite_slug)
                        if microsite and microsite.pubmed_search_terms:
                            if len(microsite.pubmed_search_terms) > 0:
                                logger.debug(
                                    f"Adding {len(microsite.pubmed_search_terms)} microsite PubMed search terms for Google Scholar for {denial.microsite_slug}"
                                )
                                queries.update(microsite.pubmed_search_terms)
                    except Exception as e:
                        logger.opt(exception=True).warning(
                            f"Failed to load microsite PubMed search terms: {e}"
                        )

                for since in self.since_list:
                    for query in queries:
                        count = 0
                        if query is None or query.strip() == "":
                            continue
                        new_article_ids = await self.find_google_scholar_article_ids_for_query(
                            query, since=since
                        )
                        for article_id in new_article_ids:
                            if article_id not in unique_article_ids:
                                count = count + 1
                                unique_article_ids.add(article_id)
                                article_ids.append(article_id)
                                if count >= PER_QUERY:
                                    break

                # Retrieve articles from database
                for article_id in article_ids:
                    mini_article = await GoogleScholarMiniArticle.objects.filter(
                        article_id=article_id
                    ).afirst()
                    if mini_article:
                        articles.append(mini_article)

        except asyncio.TimeoutError as e:
            logger.debug(
                f"Timeout in find_google_scholar_articles_for_denial: {e} so far got {articles}"
            )
        except asyncio.exceptions.CancelledError:
            logger.opt(exception=True).debug(
                f"Cancelled in find_google_scholar_articles_for_denial; so far got {articles}"
            )
            pass
        except Exception as e:
            logger.opt(exception=True).debug(f"Unexpected error {e}")
            raise e
        
        articles_json = json.dumps(list(map(lambda a: a.article_id, articles)))
        await GoogleScholarQueryData.objects.acreate(
            denial_id=denial,
            articles=articles_json,
            query=f"{denial.procedure or ''} {denial.diagnosis or ''}".strip()
            or "denial_articles",
        )
        logger.debug(f"Found {len(articles)} Google Scholar articles for denial {denial}")
        return articles

    async def find_context_for_denial(self, denial: Denial, timeout=70.0) -> str:
        result = await self._find_context_for_denial(denial, timeout)
        if result is not None and len(result) > 1:
            # Store in a new field if needed - for now we'll skip this
            # Could add google_scholar_context field to Denial model
            pass
        return result

    async def _find_context_for_denial(self, denial: Denial, timeout=70.0) -> str:
        """
        Kind of hacky RAG routine that uses Google Scholar.
        Similar to PubMed version but for Google Scholar articles.
        """
        procedure_opt = denial.procedure if denial.procedure else ""
        diagnosis_opt = denial.diagnosis if denial.diagnosis else ""
        query = f"{procedure_opt} {diagnosis_opt}".strip()
        articles: list[GoogleScholarArticleSummarized] = []
        missing_article_ids: list[str] = []

        try:
            async with async_timeout(timeout):
                # Check if the denial has specific Google Scholar IDs selected already
                selected_article_ids: Optional[list[str]] = None
                # For now, we don't have a google_scholar_ids_json field on denial
                # So we'll always search
                
                if not selected_article_ids or len(selected_article_ids) == 0:
                    logger.debug(
                        f"No pre-selected articles found, searching for Google Scholar articles"
                    )
                    if not query or query.strip() == "":
                        return ""  # Return empty string if no query available

                    possible_articles = await asyncio.wait_for(
                        self.find_google_scholar_articles_for_denial(
                            denial, timeout=(timeout / 2.5)
                        ),
                        timeout=timeout / 1.5,
                    )

                    selected_article_ids = list(map(lambda x: x.article_id, possible_articles))

                # Directly fetch the selected articles from the database
                articles = [
                    article
                    async for article in GoogleScholarArticleSummarized.objects.filter(
                        article_id__in=selected_article_ids
                    )
                ]
                logger.debug(
                    f"Found {len(articles)} pre-existing summarized articles in the database"
                )

                # If we couldn't find all the articles in the database, try to create them
                if articles and len(articles) < len(selected_article_ids):
                    for article_id in selected_article_ids:
                        if not any(a.article_id == article_id for a in articles):
                            missing_article_ids.append(article_id)
                elif articles and len(articles) == len(selected_article_ids):
                    missing_article_ids = []
                else:
                    missing_article_ids = selected_article_ids

                if missing_article_ids and len(missing_article_ids) > 0:
                    logger.debug(f"Creating summaries for {len(missing_article_ids)} missing articles")
                    # Fetch in-order so we can be interrupted
                    for article_id in missing_article_ids:
                        articles.extend(await self.get_articles([article_id]))
        except asyncio.TimeoutError as e:
            logger.debug(f"Timed out processing articles so far.")
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

        # Format the articles for context
        if articles:
            logger.debug("Making single context input")
            joined_contexts = "\n".join(
                self.format_article_short(article) for article in articles
            )
            if len(joined_contexts) < 100:
                logger.debug("Not much input, skipping summary step.")
                return joined_contexts
            r: Optional[str] = await ml_router.summarize(
                title="Combined contexts", text=joined_contexts
            )
            logger.debug("Summarization complete!")
            if r is None:
                return joined_contexts
            else:
                return r
        else:
            return ""

    @staticmethod
    def format_article_short(article: GoogleScholarArticleSummarized) -> str:
        """Helper function to format an article for context."""
        summary = None
        if article.basic_summary:
            summary = article.basic_summary
        elif article.snippet:
            summary = article.snippet[0:500]
        summary_opt = f"summary {summary}" if summary else ""
        url_opt = f"from {article.article_url}" if article.article_url else ""
        cited_opt = f"cited by {article.cited_by_count}" if article.cited_by_count else ""
        return f"Google Scholar title {article.title} {url_opt} {cited_opt} {summary_opt}"

    async def get_articles(
        self, article_ids: List[str]
    ) -> List[GoogleScholarArticleSummarized]:
        """Get Google Scholar articles by their IDs."""
        if not article_ids:
            return []

        scholar_docs: List[GoogleScholarArticleSummarized] = []
        for article_id in article_ids:
            if article_id is None or article_id == "":
                continue

            try:
                # Look for existing articles in the database first
                matching_articles = GoogleScholarArticleSummarized.objects.filter(article_id=article_id)
                if await matching_articles.aexists():
                    article = await matching_articles.afirst()
                    if article is not None:
                        scholar_docs.append(article)
                else:
                    # Article not in database, create summarized version from mini article
                    article = await self.do_article_summary(article_id)
                    if article is not None:
                        scholar_docs.append(article)
            except Exception as e:
                logger.debug(f"Skipping {article_id}: {e}")

        return scholar_docs

    async def do_article_summary(self, article_id: str) -> Optional[GoogleScholarArticleSummarized]:
        """Create a summarized version of a Google Scholar article."""
        article: Optional[GoogleScholarArticleSummarized] = (
            await GoogleScholarArticleSummarized.objects.filter(
                article_id=article_id,
                basic_summary__isnull=False,
            ).afirst()
        )

        if article is None:
            try:
                # Get the mini article
                mini_article = await GoogleScholarMiniArticle.objects.filter(
                    article_id=article_id
                ).afirst()
                
                if mini_article:
                    title = mini_article.title or ""
                    snippet = mini_article.snippet or ""
                    
                    # Try to get more text from PDF if available
                    article_text = ""
                    # For now, we won't attempt PDF extraction as it's complex
                    # Future improvement: download and extract PDF text
                    
                    if title or snippet:
                        article = await GoogleScholarArticleSummarized.objects.acreate(
                            article_id=article_id,
                            title=title,
                            snippet=snippet,
                            publication_info=mini_article.publication_info or "",
                            text=article_text,
                            article_url=mini_article.article_url,
                            pdf_file=mini_article.pdf_file,
                            cited_by_count=mini_article.cited_by_count,
                            cited_by_link=mini_article.cited_by_link,
                            basic_summary=await ml_router.summarize(
                                title=title,
                                abstract=snippet,
                                text=article_text,
                            ),
                        )
                        return article
            except Exception as e:
                logger.opt(exception=True).debug(f"Error in do_article_summary for {article_id}: {e}")
                return None

        return article


# Create a singleton instance
google_scholar_tools = GoogleScholarTools()

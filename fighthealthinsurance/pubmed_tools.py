from fighthealthinsurance.models import (
    PubMedArticleSummarized,
    PubMedQueryData,
    PubMedMiniArticle,
)
from asgiref.sync import sync_to_async, async_to_sync
from fighthealthinsurance.utils import pubmed_fetcher
from .utils import markdown_escape
from concurrent.futures import Future
from metapub import FindIt
# Removing stopit imports
import asyncio
from .models import Denial
import json
import PyPDF2
import requests
import aiohttp
from fighthealthinsurance.ml.ml_router import ml_router
import tempfile
from typing import List, Optional, Dict, Tuple, Any, Set
from .exec import pubmed_executor
import subprocess
from loguru import logger
import eutils
from datetime import datetime, timedelta
import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


PER_QUERY = 3


class PubMedTools(object):
    # Rough bias to "recent" articles
    since_list = ["2024", None]

    async def find_pubmed_article_ids_for_query(
        self,
        query: str,
        since: Optional[str] = None,
        timeout: float = 60.0,
    ) -> List[str]:
        """
        Find PubMed article IDs for a given query. May pull from database.
        Args:
            query: The search query for PubMed
            since: The year to start searching from (optional)
            timeout: Maximum time to spend searching

        Returns:
            List of PubMed IDs (PMIDs) matching the query
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
        return pmids

    async def find_pubmed_articles_for_denial(
        self, denial: Denial, timeout=70.0
    ) -> List[PubMedMiniArticle]:
        pmids: List[str] = []
        articles: List[PubMedMiniArticle] = []
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
                                url = None
                                try:
                                    logger.debug(f"Looking for {pmid} with findit")
                                    src = await asyncio.wait_for(
                                        sync_to_async(FindIt)(pmid),
                                        timeout=timeout / 5.0,
                                    )
                                    logger.debug(f"Found it {src}")
                                    url = src.url
                                except Exception:
                                    logger.debug("Findit failed.")
                                mini_article = await PubMedMiniArticle.objects.acreate(
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
                                articles.append(mini_article)
                        except Exception as e:
                            logger.opt(exception=True).debug(
                                f"Error fetching article {pmid}: {e}"
                            )
        except asyncio.TimeoutError as e:
            logger.debug(
                f"Timeout in find_pubmed_articles_for_denial: {e} so far got {articles}"
            )
        except Exception as e:
            logger.opt(exception=True).debug(f"Unexpected error {e}")
            raise e
        logger.debug(f"Found {articles}")
        return articles

    async def find_context_for_denial(self, denial: Denial, timeout=60.0) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
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
                        if selected_pmids:  # Check if not None and not empty
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
                        timeout=timeout / 2.0,
                    )

                    selected_pmids = list(map(lambda x: x.pmid, possible_articles))

                denial.pubmed_ids_json = selected_pmids
                logger.debug(f"Updating denial to have some context selected...")
                await denial.asave()
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
            logger.debug(
                f"Timeout in find_context_for_denial: {e} so far got {articles}"
            )
        except Exception as e:
            logger.opt(exception=True).debug("Non-timeout error -- {e}?")
            raise e

        # Format the articles for context
        if articles:
            logger.debug("Making single context input")
            joined_contexts = "\n".join(
                self.format_article_short(article) for article in articles
            )
            if len(joined_contexts) < 100:
                logger.debug("Not much input, skpping summary step.")
                return joined_contexts
            r = await ml_router.summarize(
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
        """Helper function to format an article for context."""
        summary = None
        if article.basic_summary:
            summary = article.basic_summary
        elif article.abstract:
            summary = article.abstract[0:500]
        summary_opt = "summary {summary}" if summary else ""
        url_opt = f"from {article.article_url}" if article.article_url else ""
        return f"PubMed DOI {article.doi} title {article.title} {url_opt} {summary_opt}"

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
                try:
                    src = FindIt(article_id)
                    url = src.url

                    if url is not None:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as response:
                                response.raise_for_status()
                                if (
                                    ".pdf" in url
                                    or response.headers.get("Content-Type")
                                    == "application/pdf"
                                ):
                                    with tempfile.NamedTemporaryFile(
                                        suffix=".pdf", delete=False
                                    ) as my_data:
                                        my_data.write(await response.read())

                                        open_pdf_file = open(my_data.name, "rb")
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
                    logger.debug("Error trying to get full text")
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
                article_text = article_text.strip()

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

    async def article_as_pdf(self, article: PubMedArticleSummarized) -> Optional[str]:
        """Return the best PDF we can find of the article."""
        # First we try and fetch the article
        try:
            async with async_timeout(20.0):
                article_id = article.pmid
                url = article.article_url
                if url is not None:
                    # TODO: Update to AIO http
                    response = requests.get(url)
                    if response.ok and (
                        ".pdf" in url
                        or response.headers.get("Content-Type") == "application/pdf"
                    ):
                        with tempfile.NamedTemporaryFile(
                            prefix=f"{article_id}", suffix=".pdf", delete=False
                        ) as my_data:
                            if len(response.content) > 20:
                                my_data.write(response.content)
                                my_data.flush()
                                return my_data.name
                            else:
                                logger.debug(f"No content from fetching {url}")
        except Exception as e:
            logger.debug(f"Error {e} fetching article for {article}")
            pass

        # Backup us markdown & pandoc -- but only if we have something to write
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
            result = subprocess.run(command)
            if result.returncode == 0:
                return f"{my_data.name}.pdf"
            else:
                logger.debug(
                    f"Error processing {command} trying again with different engine"
                )
                command = [
                    "pandoc",
                    "--wrap=auto",
                    "--read=markdown",
                    "--pdf-engine=lualatex",
                    my_data.name,
                    f"-o{my_data.name}.pdf",
                ]
                result = subprocess.run(command)
                if result.returncode == 0:
                    return f"{my_data.name}.pdf"
                else:
                    logger.debug(f"Error processing {command}")
        return None

from fighthealthinsurance.models import (
    PubMedArticleSummarized,
    PubMedQueryData,
    PubMedMiniArticle,
)
from fighthealthinsurance.utils import pubmed_fetcher
from .utils import markdown_escape
from concurrent.futures import Future
from metapub import FindIt
from stopit import ThreadingTimeout as Timeout
from .models import Denial
import json
import PyPDF2
import requests
from fighthealthinsurance.ml.ml_router import ml_router
import tempfile
from typing import List, Optional, Dict, Tuple, Any, Set
from .exec import pubmed_executor
import subprocess
from loguru import logger
import eutils
from datetime import datetime, timedelta
import json


class PubMedTools(object):
    # Rough bias to "recent" articles
    since_list = ["2025", "2024", None]

    def find_pubmed_article_ids_for_query(
        self,
        query: str,
        denial: Optional[Denial] = None,
        timeout: float = 30.0,
        max_results: int = 5,
    ) -> List[str]:
        """
        Find PubMed article IDs for a given query.

        Args:
            query: The search query for PubMed
            denial: Optional denial to associate with the query for caching
            timeout: Maximum time to spend searching
            max_results: Maximum number of results to return per "since" value (default: 5)

        Returns:
            List of PubMed IDs (PMIDs) matching the query
        """
        pmids: List[str] = []
        unique_pmids: Set[str] = set()

        with Timeout(timeout) as _timeout_ctx:
            # Check if we already have query results from the last month
            month_ago = datetime.now() - timedelta(days=30)
            existing_queries = PubMedQueryData.objects.filter(
                query=query, created__gte=month_ago
            ).order_by("-created")

            if existing_queries.exists():
                # Use cached query results
                for query_data in existing_queries:
                    if query_data.articles:
                        try:
                            article_ids = json.loads(
                                query_data.articles.replace("\x00", "")
                            )
                            # Add only new PMIDs to avoid duplicates
                            for pmid in article_ids:
                                if pmid not in unique_pmids:
                                    unique_pmids.add(pmid)
                                    pmids.append(pmid)
                        except json.JSONDecodeError:
                            logger.error(
                                f"Error parsing cached articles JSON for {query}"
                            )
            else:
                # No cached results, fetch new ones
                for since in self.since_list:
                    logger.debug(
                        f"Fetching PubMed articles for query '{query}' since {since}"
                    )
                    fetched_pmids = pubmed_fetcher.pmids_for_query(
                        query, since=since, max_results=max_results
                    )
                    if fetched_pmids:
                        # Store the results in the database for future use
                        try:
                            articles_json = json.dumps(fetched_pmids).replace(
                                "\x00", ""
                            )
                            PubMedQueryData.objects.create(
                                query=query,
                                since=since,
                                articles=articles_json,
                                denial_id=denial,
                            ).save()

                            # Add only new PMIDs to avoid duplicates
                            for pmid in fetched_pmids:
                                if pmid not in unique_pmids:
                                    unique_pmids.add(pmid)
                                    pmids.append(pmid)
                        except Exception as e:
                            logger.error(f"Error saving PubMed query data: {e}")

        # Return limited number to prevent overwhelming the system
        return pmids[:20]  # Limit to top 20

    def find_candidate_articles_for_denial(
        self, denial: Denial, timeout=60.0, max_results: int = 5
    ) -> List[PubMedMiniArticle]:
        """Find candidate articles for a given denial."""
        result_articles: List[PubMedMiniArticle] = []

        # Build the query from the denial information
        query = f"{denial.procedure} {denial.diagnosis}"

        # First get the PMIDs
        pmids = self.find_pubmed_article_ids_for_query(
            query, denial, timeout=timeout / 2, max_results=max_results
        )

        # Then fetch article details for each PMID
        with Timeout(timeout / 2) as _timeout_ctx:
            # Check if articles already exist in database
            for pmid in pmids:
                mini_article = PubMedMiniArticle.objects.filter(pmid=pmid).first()

                if mini_article:
                    # Use existing article from database
                    result_articles.append(mini_article)
                else:
                    # Create a new mini article
                    try:
                        fetched = pubmed_fetcher.article_by_pmid(pmid)
                        if fetched:
                            src = FindIt(pmid)
                            url = src.url
                            mini_article = PubMedMiniArticle.objects.create(
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
                            result_articles.append(mini_article)
                    except Exception as e:
                        logger.error(f"Error fetching article {pmid}: {e}")

        return result_articles

    def find_context_for_denial(
        self, denial: Denial, timeout=60.0, max_results: int = 5
    ) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
        # Check if the denial has specific pubmed IDs selected
        selected_pmids = []
        if denial.pubmed_ids_json and denial.pubmed_ids_json.strip():
            try:
                selected_pmids = json.loads(denial.pubmed_ids_json)
            except json.JSONDecodeError:
                logger.error(
                    f"Error parsing pubmed_ids_json for denial {denial.denial_id}"
                )

        # If we have selected pmids, use those preferentially
        if selected_pmids:
            logger.info(
                f"Using {len(selected_pmids)} pre-selected PubMed articles for denial {denial.denial_id}"
            )
            selected_articles = []
            for pmid in selected_pmids:
                article = self.do_article_summary(
                    pmid, f"{denial.procedure} {denial.diagnosis}"
                )
                if article:
                    selected_articles.append(article)
            if selected_articles:
                return "\n".join(
                    self.format_article_short(article) for article in selected_articles
                )

        # PubMed - fallback to regular search if no specific articles selected
        article_futures: list[Future[Optional[PubMedArticleSummarized]]] = []

        # First get query and PMIDs
        query = f"{denial.procedure} {denial.diagnosis}"
        pmids = self.find_pubmed_article_ids_for_query(
            query, denial, timeout=timeout / 3, max_results=max_results
        )

        # Now process the articles
        with Timeout(timeout * 2 / 3) as _timeout_ctx:
            for article_id in pmids[0:max_results]:
                logger.debug(f"Loading {article_id}")
                article_futures.append(
                    pubmed_executor.submit(self.do_article_summary, article_id, query)
                )

        # Collect the results
        result_articles: list[PubMedArticleSummarized] = []

        # Get the articles that we've summarized
        t = 10
        for f in article_futures:
            try:
                result = f.result(timeout=t)
                if result is not None:
                    result_articles.append(result)
                t = t - 1
            except Exception as e:
                logger.debug(
                    f"Skipping appending article from {f} due to {e} of {type(e)}"
                )

        if len(result_articles) > 0:
            return "\n".join(
                self.format_article_short(article) for article in result_articles
            )
        else:
            return ""

    @staticmethod
    def format_article_short(article) -> str:
        """Helper function to format an article for context."""
        return f"PubMed DOI {article.doi} title {article.title} summary {article.basic_summary}"

    def get_articles(self, pubmed_ids: List[str]) -> List[PubMedArticleSummarized]:
        pubmed_docs: List[PubMedArticleSummarized] = []
        for pmid in pubmed_ids:
            if pmid is None or pmid == "":
                continue
            try:
                pubmed_docs.append(
                    PubMedArticleSummarized.objects.filter(pmid=pmid).first()
                )
            except:
                try:
                    fetched = pubmed_fetcher.article_by_pmid(pmid)
                    if fetched is not None:
                        article = PubMedArticleSummarized.objects.create(
                            pmid=pmid,
                            doi=fetched.doi,
                            title=fetched.title.replace("\x00", ""),
                            abstract=fetched.abstract.replace("\x00", ""),
                            text=fetched.content.text.replace("\x00", ""),
                        )
                        pubmed_docs.append(article)
                except:
                    logger.debug(f"Skipping {pmid}")
        return [doc for doc in pubmed_docs if doc is not None]

    def do_article_summary(
        self, article_id, query
    ) -> Optional[PubMedArticleSummarized]:
        possible_articles = PubMedArticleSummarized.objects.filter(
            pmid=article_id,
            basic_summary__isnull=False,
        )[:1]
        article = None
        if len(possible_articles) > 0:
            article = possible_articles[0]
        url = None
        if article is None:
            fetched = pubmed_fetcher.article_by_pmid(article_id)
            src = FindIt(article_id)
            url = src.url
            article_text = ""
            if url is not None:
                response = requests.get(url)
                if (
                    ".pdf" in url
                    or response.headers.get("Content-Type") == "application/pdf"
                ):
                    with tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False
                    ) as my_data:
                        my_data.write(response.content)
                        open_pdf_file = open(my_data.name, "rb")
                        read_pdf = PyPDF2.PdfReader(open_pdf_file)
                        if read_pdf.is_encrypted:
                            read_pdf.decrypt("")
                            for page in read_pdf.pages:
                                article_text += page.extract_text()
                        else:
                            for page in read_pdf.pages:
                                article_text += page.extract_text()
                else:
                    article_text += response.text
            else:
                article_text = fetched.content.text
            if fetched is not None and (
                fetched.abstract is not None or article_text is not None
            ):
                article = PubMedArticleSummarized.objects.create(
                    pmid=article_id,
                    doi=fetched.doi,
                    title=fetched.title,
                    abstract=fetched.abstract,
                    text=article_text,
                    query=query,
                    article_url=url,
                    basic_summary=ml_router.summarize(
                        query=query, abstract=fetched.abstract, text=article_text
                    ),
                )
                return article
            else:
                logger.debug(f"Skipping {fetched}")
                return None
        return None

    def article_as_pdf(self, article: PubMedArticleSummarized) -> Optional[str]:
        """Return the best PDF we can find of the article."""
        # First we try and fetch the article
        try:
            with Timeout(15.0) as _timeout_ctx:
                article_id = article.pmid
                url = article.article_url
                if url is not None:
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

        article_id = article.pmid
        markdown_text = f"# {markdown_escape(article.title)} \n\n PMID {article.pmid} / DOI {article.doi}\n\n{markdown_escape(article.abstract)}\n\n---{markdown_escape(article.text)}"
        with tempfile.NamedTemporaryFile(
            prefix=f"{article_id}",
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

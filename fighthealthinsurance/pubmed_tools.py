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
        since: Optional[str],
        timeout: float = 30.0,
    ) -> List[str]:
        """
        Find PubMed article IDs for a given query. May pull from database.
        Args:
            query: The search query for PubMed
            denial: Optional denial to associate with the query for caching
            timeout: Maximum time to spend searching

        Returns:
            List of PubMed IDs (PMIDs) matching the query
        """
        pmids: List[str] = []

        try:
            with Timeout(timeout) as _timeout_ctx:
                # Check if we already have query results from the last month
                month_ago = datetime.now() - timedelta(days=30)
                existing_queries = PubMedQueryData.objects.filter(
                    query=query,
                    created__gte=month_ago,
                    since=since,
                ).order_by("-created")

                if existing_queries.exists():
                    # Use cached query results
                    for query_data in existing_queries:
                        if query_data.articles:
                            try:
                                article_ids: list[str] = json.loads(
                                    query_data.articles.replace("\x00", "")
                                )  # type: ignore
                                # Add only new PMIDs to avoid duplicates
                                return article_ids
                                # Continue from here
                                continue
                            except json.JSONDecodeError:
                                logger.error(
                                    f"Error parsing cached articles JSON for {query}"
                                )
                fetched_pmids = pubmed_fetcher.pmids_for_query(query, since=since)
                if fetched_pmids:
                    # Sometimes we get nulls...
                    articles_json = json.dumps(fetched_pmids).replace("\x00", "")
                    PubMedQueryData.objects.create(
                        query=query,
                        since=since,
                        articles=articles_json,
                    ).save()
                    pmids.extend(fetched_pmids)
        except:
            # We might timeout
            pass
        return pmids

    def find_pubmed_articles_for_denial(
        self, denial: Denial, timeout=90.0
    ) -> List[PubMedMiniArticle]:
        pmids: List[str] = []
        # Allow us to remove duplicates while preserving order
        unique_pmids: Set[str] = set()
        queries = [
            f"{denial.procedure} {denial.diagnosis}",
        ]
        for since in self.since_list:
            count = 0
            for query in queries:
                if query is None or query.strip() == "":
                    continue
                new_pmids = self.find_pubmed_article_ids_for_query(query, since=since)
                for pmid in new_pmids:
                    if pmid not in unique_pmids:
                        count = count + 1
                        unique_pmids.add(pmid)
                        pmids.append(pmid)
                        # Add 5 unique per query set
                        if count >= 5:
                            break

        # Check if articles already exist in database
        articles: List[PubMedMiniArticle] = []
        for pmid in pmids:
            mini_article = PubMedMiniArticle.objects.filter(pmid=pmid).first()
            if mini_article:
                articles.append(mini_article)
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
                        articles.append(mini_article)
                except Exception as e:
                    logger.error(f"Error fetching article {pmid}: {e}")
        return articles

    def find_context_for_denial(self, denial: Denial, timeout=60.0) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
        query = f"{denial.procedure} {denial.diagnosis}"
        article_futures: list[Future[Optional[PubMedArticleSummarized]]] = []
        articles: list[PubMedArticleSummarized] = []
        # Check if the denial has specific pubmed IDs selected already
        selected_pmids: Optional[list[str]] = None
        if denial.pubmed_ids_json and denial.pubmed_ids_json.strip():
            try:
                selected_pmids = json.loads(denial.pubmed_ids_json)  # type: ignore
            except json.JSONDecodeError:
                logger.error(
                    f"Error parsing pubmed_ids_json for denial {denial.denial_id}"
                )

        # If we have selected pmids, use those preferentially
        if selected_pmids:
            logger.info(
                f"Using {len(selected_pmids)} pre-selected PubMed articles for denial {denial.denial_id}"
            )
            for pmid in selected_pmids:
                article_futures.append(
                    pubmed_executor.submit(self.do_article_summary, pmid)
                )
        else:
            # PubMed - fallback to regular search if no specific articles selected
            mini_articles = self.find_pubmed_articles_for_denial(
                denial, timeout=(timeout / 2.0)
            )
            for article in mini_articles:
                article_id = article.pmid
                logger.debug(f"Loading {article_id}")
                article_futures.append(
                    pubmed_executor.submit(self.do_article_summary, article_id)
                )
        # Get the articles that we've summarized
        t = timeout / 2.0
        for f in article_futures:
            try:
                result = f.result(timeout=t)
                if result is not None:
                    articles.append(result)
                t = t - 1
            except Exception as e:
                logger.debug(
                    f"Skipping appending article from {f} due to {e} of {type(e)}"
                )
        if len(articles) > 0:
            return "\n".join(self.format_article_short(article) for article in articles)
        else:
            return ""

    @staticmethod
    def format_article_short(article: PubMedArticleSummarized) -> str:
        """Helper function to format an article for context."""
        return f"PubMed DOI {article.doi} title {article.title} summary {article.basic_summary}"

    def get_articles(self, pubmed_ids: List[str]) -> List[PubMedArticleSummarized]:
        pubmed_docs: List[PubMedArticleSummarized] = []
        for pmid in pubmed_ids:
            if pmid is None or pmid == "":
                continue
            try:
                pubmed_docs.append(
                    PubMedArticleSummarized.objects.filter(pmid == pmid)[0]
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
        return pubmed_docs

    def do_article_summary(self, article_id) -> Optional[PubMedArticleSummarized]:
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
                    article_url=url,
                    basic_summary=ml_router.summarize(
                        abstract=fetched.abstract, text=article_text
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

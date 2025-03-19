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


class PubMedTools(object):
    # Rough bias to "recent" articles
    since_list = ["2025", "2024", None]

    def find_pubmed_article_ids_for_query(
        self,
        query: str,
        since: Optional[str] = None,
        timeout: float = 30.0,
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
                                )
                                # Return the cached IDs
                                return article_ids
                            except json.JSONDecodeError:
                                logger.error(
                                    f"Error parsing cached articles JSON for {query}"
                                )

                # If no cache or cache error, fetch from PubMed API
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

                # If we didn't get any results, but we're not restricted by 'since',
                # try querying with each since value in our since_list until we find results
                if not pmids and since is None:
                    for year in self.since_list:
                        if year is not None:
                            # Don't re-try with None since we already did that
                            logger.debug(
                                f"No results for {query}, trying with since={year}"
                            )
                            year_pmids = pubmed_fetcher.pmids_for_query(
                                query, since=year
                            )
                            if year_pmids:
                                # Cache the results
                                articles_json = json.dumps(year_pmids).replace(
                                    "\x00", ""
                                )
                                PubMedQueryData.objects.create(
                                    query=query,
                                    since=year,
                                    articles=articles_json,
                                ).save()
                                pmids.extend(year_pmids)
                                break
        except Exception as e:
            # We might timeout
            logger.debug(f"Error or timeout in find_pubmed_article_ids_for_query: {e}")
            pass
        return pmids

    def find_pubmed_articles_for_denial(
        self, denial: Denial, timeout=30.0
    ) -> List[PubMedMiniArticle]:
        pmids: List[str] = []
        articles: List[PubMedMiniArticle] = []
        try:
            with Timeout(timeout) as _timeout_ctx:
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
                        new_pmids = self.find_pubmed_article_ids_for_query(
                            query, since=since
                        )
                        for pmid in new_pmids:
                            if pmid not in unique_pmids:
                                count = count + 1
                                unique_pmids.add(pmid)
                                pmids.append(pmid)
                                # Add 5 unique per query set
                                if count >= 5:
                                    break

                # Check if articles already exist in database
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
        except Timeout as e:
            logger.debug(
                f"Timeout in find_pubmed_articles_for_denial: {e} so far got {articles}"
            )
        return articles

    def find_context_for_denial(self, denial: Denial, timeout=30.0) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
        procedure_opt = denial.procedure if denial.procedure else ""
        diagnosis_opt = denial.diagnosis if denial.diagnosis else ""
        query = f"{procedure_opt} {diagnosis_opt}".strip()
        articles: list[PubMedArticleSummarized] = []
        missing_pmids: list[str] = []

        try:
            with Timeout(timeout) as _timeout_ctx:
                # Check if the denial has specific pubmed IDs selected already
                selected_pmids: Optional[list[str]] = None
                if denial.pubmed_ids_json and denial.pubmed_ids_json.strip():
                    try:
                        selected_pmids = json.loads(denial.pubmed_ids_json)
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

                    selected_pmids = list(
                        map(
                            lambda x: x.pmid,
                            self.find_pubmed_articles_for_denial(
                                denial, timeout=(timeout / 2.0)
                            ),
                        )
                    )

                # Directly fetch the selected articles from the database
                articles = list(
                    PubMedArticleSummarized.objects.filter(pmid__in=selected_pmids)
                )
                logger.debug(
                    f"Found {len(articles)} pre-selected articles in the database"
                )

                # If we couldn't find all the articles in the database, try to fetch them
                missing_pmids = []
                if articles and len(articles) < len(selected_pmids):
                    for pmid in selected_pmids:
                        if not any(a.pmid == pmid for a in articles):
                            missing_pmids.append(pmid)
                else:
                    missing_pmids = selected_pmids

                if missing_pmids and len(missing_pmids) > 0:
                    logger.debug(f"Fetching {len(missing_pmids)} missing articles")
                # Fetch in-order so we can be interrupted
                for pmid in missing_pmids:
                    articles.extend(self.get_articles([pmid]))
        except Timeout as e:
            logger.debug(
                f"Timeout in find_context_for_denial: {e} so far got {articles}"
            )

        # Format the articles for context
        if articles:
            return "\n".join(self.format_article_short(article) for article in articles)
        else:
            return ""

    @staticmethod
    def format_article_short(article: PubMedArticleSummarized) -> str:
        """Helper function to format an article for context."""
        return f"PubMed DOI {article.doi} title {article.title} summary {article.basic_summary}"

    def get_articles(self, pubmed_ids: List[str]) -> List[PubMedArticleSummarized]:
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
                if matching_articles.exists():
                    article = matching_articles.first()
                    if article is not None:
                        pubmed_docs.append(article)
                else:
                    # Article not in database, fetch it
                    fetched = pubmed_fetcher.article_by_pmid(pmid)
                    if fetched is not None:
                        article = PubMedArticleSummarized.objects.create(
                            pmid=pmid,
                            doi=fetched.doi if hasattr(fetched, "doi") else "",
                            title=(
                                fetched.title.replace("\x00", "")
                                if hasattr(fetched, "title") and fetched.title
                                else ""
                            ),
                            abstract=(
                                fetched.abstract.replace("\x00", "")
                                if hasattr(fetched, "abstract") and fetched.abstract
                                else ""
                            ),
                            text=(
                                fetched.content.text.replace("\x00", "")
                                if hasattr(fetched, "content")
                                and hasattr(fetched.content, "text")
                                and fetched.content.text
                                else ""
                            ),
                        )
                        pubmed_docs.append(article)
            except Exception as e:
                logger.debug(f"Skipping {pmid}: {e}")

        return pubmed_docs

    def do_article_summary(self, article_id) -> Optional[PubMedArticleSummarized]:
        possible_articles = PubMedArticleSummarized.objects.filter(
            pmid=article_id,
            basic_summary__isnull=False,
        )[:1]

        article = None
        if possible_articles:
            article = possible_articles[0]

        if article is None:
            try:
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
                elif (
                    fetched
                    and hasattr(fetched, "content")
                    and hasattr(fetched.content, "text")
                ):
                    article_text = fetched.content.text

                if fetched is not None and (
                    (hasattr(fetched, "abstract") and fetched.abstract) or article_text
                ):
                    article = PubMedArticleSummarized.objects.create(
                        pmid=article_id,
                        doi=fetched.doi if hasattr(fetched, "doi") else "",
                        title=fetched.title if hasattr(fetched, "title") else "",
                        abstract=(
                            fetched.abstract if hasattr(fetched, "abstract") else ""
                        ),
                        text=article_text,
                        article_url=url,
                        basic_summary=ml_router.summarize(
                            abstract=(
                                fetched.abstract if hasattr(fetched, "abstract") else ""
                            ),
                            text=article_text,
                        ),
                    )
                    return article
            except Exception as e:
                logger.debug(f"Error in do_article_summary for {article_id}: {e}")
                return None

        return article

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

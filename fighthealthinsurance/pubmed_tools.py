from fighthealthinsurance.models import (
    PubMedArticleSummarized,
    PubMedQueryData,
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
from typing import List, Optional
from .exec import pubmed_executor
import subprocess
from loguru import logger


class PubMedTools(object):
    def find_context_for_denial(self, denial: Denial) -> str:
        """
        Kind of hacky RAG routine that uses PubMed.
        """
        # PubMed
        pmids = None
        pmid_text: list[str] = []
        article_futures: list[Future[Optional[PubMedArticleSummarized]]] = []
        with Timeout(15.0) as _timeout_ctx:
            query = f"{denial.procedure} {denial.diagnosis}"
            pmids = pubmed_fetcher.pmids_for_query(query)
            # Sometimes we get nulls...
            articles_json = json.dumps(pmids).replace("\x00", "")
            PubMedQueryData.objects.create(
                query=query,
                articles=articles_json,
                denial_id=denial,
            ).save()
            for article_id in pmids[0:10]:
                logger.debug(f"Loading {article_id}")
                article_futures.append(
                    pubmed_executor.submit(self.do_article_summary, article_id, query)
                )

        def format_article_short(article) -> str:
            return f"PubMed DOI {article.doi} title {article.title} summary {article.basic_summary}"

        articles: list[PubMedArticleSummarized] = []
        # Get the articles that we've summarized
        t = 10
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
            return "\n".join(map(format_article_short, articles))
        else:
            return ""

    def get_articles(self, pubmed_ids: List[str]) -> List[PubMedArticleSummarized]:
        pubmed_docs: List[PubMedArticleSummarized] = []
        for pmid in pubmed_ids:
            if pmid is None or pmid == "":
                continue
            try:
                article = PubMedArticleSummarized.objects.filter(pmid=pmid).first()
                if article is not None:
                    pubmed_docs.append(article)
                else:
                    try:
                        fetched = pubmed_fetcher.article_by_pmid(pmid)
                        if fetched is not None:
                            title = fetched.title.replace("\x00", "") if fetched.title else ""
                            abstract = fetched.abstract.replace("\x00", "") if fetched.abstract else ""
                            text = fetched.content.text.replace("\x00", "") if fetched.content and fetched.content.text else ""
                            
                            article = PubMedArticleSummarized.objects.create(
                                pmid=pmid,
                                doi=fetched.doi or "",
                                title=title,
                                abstract=abstract,
                                text=text,
                            )
                            pubmed_docs.append(article)
                    except Exception as e:
                        logger.debug(f"Failed to fetch article {pmid}: {e}")
            except Exception as e:
                logger.debug(f"Error retrieving article {pmid} from database: {e}")
        return pubmed_docs

    def do_article_summary(
        self, article_id, query
    ) -> Optional[PubMedArticleSummarized]:
        try:
            possible_articles = PubMedArticleSummarized.objects.filter(
                pmid=article_id,
                basic_summary__isnull=False,
            )[:1]
            article = possible_articles.first()

            if article is None:
                fetched = pubmed_fetcher.article_by_pmid(article_id)
                if fetched is None:
                    logger.debug(f"Could not fetch article {article_id}")
                    return None
                    
                src = FindIt(article_id)
                url = src.url
                article_text = ""
                
                if url is not None:
                    try:
                        response = requests.get(url, timeout=10)
                        if (
                            ".pdf" in url
                            or response.headers.get("Content-Type") == "application/pdf"
                        ):
                            with tempfile.NamedTemporaryFile(
                                suffix=".pdf", delete=False
                            ) as my_data:
                                my_data.write(response.content)

                                with open(my_data.name, "rb") as open_pdf_file:
                                    read_pdf = PyPDF2.PdfReader(open_pdf_file)
                                    if read_pdf.is_encrypted:
                                        try:
                                            read_pdf.decrypt("")
                                        except Exception as e:
                                            logger.debug(f"Could not decrypt PDF: {e}")
                                            
                                    for page in read_pdf.pages:
                                        page_text = page.extract_text() or ""
                                        article_text += page_text
                        else:
                            article_text += response.text
                    except Exception as e:
                        logger.debug(f"Error processing URL {url}: {e}")
                else:
                    article_text = fetched.content.text if fetched.content and fetched.content.text else ""

                abstract = fetched.abstract or ""
                title = fetched.title or ""
                doi = fetched.doi or ""
                
                if abstract or article_text:
                    try:
                        article = PubMedArticleSummarized.objects.create(
                            pmid=article_id,
                            doi=doi,
                            title=title,
                            abstract=abstract,
                            text=article_text,
                            query=query,
                            article_url=url,
                            basic_summary=ml_router.summarize(
                                query=query, abstract=abstract, text=article_text
                            ),
                        )
                        return article
                    except Exception as e:
                        logger.debug(f"Failed to create article summary: {e}")
                        return None
                else:
                    logger.debug(f"Skipping article {article_id}: insufficient content")
                    return None
            return article
        except Exception as e:
            logger.debug(f"Unexpected error in do_article_summary for {article_id}: {e}")
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

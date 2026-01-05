"""
ExtraLink Pre-fetch Ray Actor.

This actor runs once at deploy time to pre-fetch external documents
from microsites and PubMed articles from microsite search terms.
"""

import os
import ray
import asyncio
import time
from loguru import logger


@ray.remote(max_restarts=-1, max_task_retries=-1)
class ExtraLinkPrefetchActor:
    """
    Ray actor that pre-fetches extralink documents and PubMed articles at deploy time.

    Unlike polling actors (which run continuously), this actor:
    1. Runs once at deploy time
    2. Fetches all pending extralink documents
    3. Pre-fetches PubMed articles from microsite search terms
    4. Exits when complete
    """

    def __init__(self):
        """Initialize the actor and Django application."""
        logger.info("Starting ExtraLink Pre-fetch Actor")

        # Initialize Django WSGI application inside the actor
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()

        self.extralink_fetched = 0
        self.extralink_failed = 0
        self.extralink_skipped = 0
        self.pubmed_fetched = 0
        self.pubmed_failed = 0

        logger.info("ExtraLink Pre-fetch Actor initialized")

    async def prefetch_all(self) -> dict:
        """
        Pre-fetch both extralinks and PubMed articles.

        Returns:
            Dict with results:
            {
                'extralinks': {...},
                'pubmed': {...},
                'total_fetched': int,
            }
        """
        logger.info("Starting pre-fetch operation")
        start_time = time.time()

        try:
            # Fetch extralinks
            extralink_result = await self._prefetch_extralinks()

            # Fetch PubMed articles
            pubmed_result = await self._prefetch_pubmed()

            elapsed = time.time() - start_time
            total_fetched = extralink_result.get("fetched", 0) + pubmed_result.get(
                "fetched", 0
            )

            logger.info(
                f"Pre-fetch completed in {elapsed:.2f}s: "
                f"{total_fetched} total documents fetched"
            )

            return {
                "extralinks": extralink_result,
                "pubmed": pubmed_result,
                "total_fetched": total_fetched,
                "duration_seconds": elapsed,
            }

        except Exception as e:
            logger.opt(exception=True).error(f"Error in pre-fetch operation: {e}")
            return {
                "extralinks": {
                    "fetched": self.extralink_fetched,
                    "failed": self.extralink_failed + 1,
                    "skipped": self.extralink_skipped,
                },
                "pubmed": {
                    "fetched": self.pubmed_fetched,
                    "failed": self.pubmed_failed + 1,
                },
                "total_fetched": self.extralink_fetched + self.pubmed_fetched,
                "error": str(e),
            }

    async def _prefetch_extralinks(self) -> dict:
        """
        Pre-fetch extralink documents from microsites.

        Returns:
            Dict with fetch statistics
        """
        logger.info("Starting extralink pre-fetch")

        try:
            from fighthealthinsurance.extralink_fetcher import ExtraLinkFetcher

            fetcher = ExtraLinkFetcher()
            result = await fetcher.prefetch_all_microsite_links()

            self.extralink_fetched = result.get("fetched", 0)
            self.extralink_failed = result.get("failed", 0)
            self.extralink_skipped = result.get("skipped", 0)

            logger.info(
                f"Extralink pre-fetch complete: {self.extralink_fetched} fetched, "
                f"{self.extralink_failed} failed, {self.extralink_skipped} skipped"
            )

            return result

        except Exception as e:
            logger.opt(exception=True).error(f"Error in extralink pre-fetch: {e}")
            return {
                "fetched": 0,
                "failed": 1,
                "skipped": 0,
                "total": 0,
                "error": str(e),
            }

    async def _prefetch_pubmed(self) -> dict:
        """
        Pre-fetch PubMed articles from microsite search terms.

        Returns:
            Dict with fetch statistics
        """
        logger.info("Starting PubMed pre-fetch")

        try:
            from fighthealthinsurance.pubmed_tools import PubMedTools
            from fighthealthinsurance.microsites import get_all_microsites
            from asgiref.sync import sync_to_async

            pubmed_tools = PubMedTools()
            microsites = await sync_to_async(get_all_microsites)()

            stats = {"fetched": 0, "failed": 0, "total": 0}

            for slug, microsite in microsites.items():
                if not microsite.pubmed_search_terms:
                    continue

                for search_term in microsite.pubmed_search_terms:
                    try:
                        # Find article IDs
                        pmids = await pubmed_tools.find_pubmed_article_ids_for_query(
                            search_term,
                            since="2020",
                        )

                        stats["total"] += len(pmids)

                        # Fetch articles (limit to 5 per search term to avoid overwhelming)
                        for pmid in pmids[:5]:
                            try:
                                # This will cache the article in the database
                                article = await pubmed_tools.do_article_summary(pmid)
                                if article:
                                    stats["fetched"] += 1
                            except Exception as e:
                                logger.debug(f"Failed to fetch PMID {pmid}: {e}")
                                stats["failed"] += 1

                    except Exception as e:
                        logger.warning(
                            f"Error pre-fetching PubMed for '{search_term}': {e}"
                        )
                        stats["failed"] += 1

            self.pubmed_fetched = stats["fetched"]
            self.pubmed_failed = stats["failed"]

            logger.info(
                f"PubMed pre-fetch complete: {stats['fetched']}/{stats['total']} articles fetched"
            )

            return stats

        except Exception as e:
            logger.opt(exception=True).error(f"Error in PubMed pre-fetch: {e}")
            return {
                "fetched": 0,
                "failed": 1,
                "total": 0,
                "error": str(e),
            }

    async def get_stats(self) -> dict:
        """
        Get current fetch statistics.

        Returns:
            Dict with current stats
        """
        return {
            "extralink_fetched": self.extralink_fetched,
            "extralink_failed": self.extralink_failed,
            "extralink_skipped": self.extralink_skipped,
            "pubmed_fetched": self.pubmed_fetched,
            "pubmed_failed": self.pubmed_failed,
        }

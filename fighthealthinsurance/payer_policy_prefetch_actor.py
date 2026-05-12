"""
Payer-Policy Pre-fetch Ray Actor.

This actor runs once at deploy time to pre-fetch the static medical-policy
index for each ``InsuranceCompany`` flagged with
``medical_policy_url_is_static_index=True`` and replace that company's
``PayerPolicyEntry`` rows with the freshly-parsed entries.

Wrapping the ingest in a Ray actor lets the deploy-time ``PREFETCH_EXTRALINKS``
job kick this off alongside the existing extralink pre-fetch instead of
blocking the job on a synchronous ``asyncio.run`` over every payer index.
"""

import os
import time

import ray
from loguru import logger


@ray.remote(max_restarts=-1, max_task_retries=-1)
class PayerPolicyPrefetchActor:
    """
    Ray actor that pre-fetches payer medical-policy indexes at deploy time.

    Like :class:`ExtraLinkPrefetchActor`, this actor:
    1. Runs once at deploy time
    2. Fetches every static medical-policy index it knows about
    3. Replaces each company's ``PayerPolicyEntry`` rows with the parsed entries
    4. Exits when complete
    """

    def __init__(self):
        """Initialize the actor and Django application."""
        logger.info("Starting Payer-Policy Pre-fetch Actor")

        # Initialize Django WSGI application inside the actor
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()

        self.fetched = 0
        self.failed = 0
        self.entries = 0

        logger.info("Payer-Policy Pre-fetch Actor initialized")

    async def prefetch_all(self) -> dict:
        """
        Pre-fetch every static payer medical-policy index.

        Returns:
            Dict with results:
            {
                'fetched': int,   # number of payers fetched successfully
                'failed': int,    # number of payers whose ingest failed
                'entries': int,   # total PayerPolicyEntry rows written
                'duration_seconds': float,
            }
        """
        logger.info("Starting payer-policy pre-fetch operation")
        start_time = time.time()

        try:
            from fighthealthinsurance.payer_policy_fetcher import PayerPolicyFetcher

            async with PayerPolicyFetcher() as fetcher:
                stats = await fetcher.ingest_all()

            self.fetched = stats.get("fetched", 0)
            self.failed = stats.get("failed", 0)
            self.entries = stats.get("entries", 0)

            elapsed = time.time() - start_time
            logger.info(
                f"Payer-policy pre-fetch completed in {elapsed:.2f}s: "
                f"{self.fetched} payers fetched, {self.failed} failed, "
                f"{self.entries} entries written"
            )

            return {
                "fetched": self.fetched,
                "failed": self.failed,
                "entries": self.entries,
                "duration_seconds": elapsed,
            }

        except Exception as e:
            logger.opt(exception=True).error(
                f"Error in payer-policy pre-fetch operation: {e}"
            )
            return {
                "fetched": self.fetched,
                "failed": self.failed + 1,
                "entries": self.entries,
                "error": str(e),
            }

    async def get_stats(self) -> dict:
        """
        Get current fetch statistics.

        Returns:
            Dict with current stats
        """
        return {
            "fetched": self.fetched,
            "failed": self.failed,
            "entries": self.entries,
        }

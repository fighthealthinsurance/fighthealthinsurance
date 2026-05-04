"""
Fetch and re-parse every static payer medical-policy index.

Idempotent: replaces each company's :class:`PayerPolicyEntry` rows with the
freshly-parsed entries from the live URL. Intended to be run at deploy
time (alongside ``launch_prefetch_actor``) and on a periodic cron so that
``payer_policy_helper`` keeps surfacing live, working URLs in appeal
prompts.
"""

import asyncio
from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.payer_policy_fetcher import PayerPolicyFetcher


class Command(BaseCommand):
    help = "Fetch and parse every payer medical-policy static index."

    def handle(self, *args: str, **options: Any) -> None:
        async def _run() -> dict[str, int]:
            async with PayerPolicyFetcher() as fetcher:
                return await fetcher.ingest_all()

        stats = asyncio.run(_run())
        summary = (
            f"Payer-policy ingest: {stats['fetched']} fetched, "
            f"{stats['failed']} failed, {stats['entries']} entries written."
        )
        # ingest_all swallows per-company exceptions and returns counts, so
        # the command always exits 0; surface partial/total failure via
        # styling so it's visible in startup logs.
        if stats["failed"] > 0 and stats["fetched"] == 0:
            self.stdout.write(self.style.ERROR(summary))
        elif stats["failed"] > 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

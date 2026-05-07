"""
Fetch and re-parse carrier-published PA requirement lists.

Idempotent: replaces each carrier's ingestion-owned
``PayerPriorAuthRequirement`` rows with the freshly-parsed entries from
the live source. Intended to run on a periodic cadence (alongside the
``PaRefreshActor``) so the lookup keeps returning live carrier rules.

Usage::

    python manage.py ingest_pa_requirements                  # all carriers
    python manage.py ingest_pa_requirements --carrier UHC    # single carrier
"""

import asyncio
from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.pa_requirements_fetcher import (
    PA_PARSERS,
    PaRequirementsFetcher,
)


class Command(BaseCommand):
    help = "Fetch and parse every carrier-published PA requirement list."

    def add_arguments(self, parser):
        parser.add_argument(
            "--carrier",
            dest="carrier",
            default=None,
            help=(
                "Limit ingest to one carrier (alias must match a key in "
                "PA_PARSERS). Default: ingest every registered carrier."
            ),
        )

    def handle(self, *args: str, **options: Any) -> None:
        carrier = options.get("carrier")

        async def _run() -> dict:
            async with PaRequirementsFetcher() as fetcher:
                if carrier:
                    return await fetcher.ingest_carrier(carrier)
                return await fetcher.ingest_all()

        if carrier and carrier not in PA_PARSERS:
            self.stdout.write(
                self.style.ERROR(
                    f"No PA parser registered for '{carrier}'. "
                    f"Known aliases: {sorted(PA_PARSERS) or '(none)'}."
                )
            )
            return

        if not PA_PARSERS:
            # Framework is in place but no parsers are registered yet —
            # surface that as a warning so operators don't think the
            # command silently succeeded.
            self.stdout.write(
                self.style.WARNING(
                    "PA-requirement parser registry is empty; nothing to ingest."
                )
            )
            return

        stats = asyncio.run(_run())

        summary = (
            f"PA requirements ingest: {stats['fetched']} carrier(s) fetched, "
            f"{stats['failed']} failed, {stats['entries']} rows upserted, "
            f"{stats['retired']} rows soft-retired."
        )
        # Per-carrier exceptions are caught inside the fetcher and returned
        # as `failed` counts; mirror ingest_payer_policy_indexes' exit-zero
        # semantics with WARNING/ERROR styling.
        if stats["failed"] > 0 and stats["fetched"] == 0:
            self.stdout.write(self.style.ERROR(summary))
        elif stats["failed"] > 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

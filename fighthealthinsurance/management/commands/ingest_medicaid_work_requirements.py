"""Check every state's Medicaid agency site for public work-requirement mentions.

Refreshes the ``work_requirement_last_checked`` / ``work_requirement_source_url``
/ ``work_requirement_mentioned`` provenance columns in
``data/medicaid_resources.csv`` in place. The curated
``work_requirement_waiver`` / ``waiver_activity`` columns are left untouched --
this is a lightweight freshness check, not a re-curation.

Usage::

    python manage.py ingest_medicaid_work_requirements
    python manage.py ingest_medicaid_work_requirements --state Georgia
    python manage.py ingest_medicaid_work_requirements --dry-run
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from django.core.management.base import BaseCommand

from fighthealthinsurance.medicaid_work_requirements_fetcher import (
    MedicaidWorkRequirementFetcher,
)


class Command(BaseCommand):
    help = (
        "Check each state's Medicaid agency homepage for work-requirement "
        "mentions and refresh the provenance columns in "
        "data/medicaid_resources.csv."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--state",
            metavar="NAME",
            action="append",
            dest="states",
            help=(
                "Restrict to one state (full name as used in the CSV, e.g. "
                "'Georgia'). May be passed multiple times. Default: every "
                "state in the CSV."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check but do NOT write the refreshed CSV.",
        )

    def handle(self, *args: str, **options: Any) -> None:
        states: Optional[list] = options.get("states")
        dry_run: bool = bool(options.get("dry_run"))

        stats = asyncio.run(self._run(states, dry_run))

        prefix = "[DRY RUN] " if dry_run else ""
        summary = (
            f"{prefix}Medicaid work-requirement check: {stats['checked']} "
            f"state site(s) checked, {stats['mentioned']} mentioned work/"
            f"community-engagement requirements, {stats['failed']} failed, "
            f"{stats['skipped']} skipped (no agency_website on file)."
        )

        if stats["failed"] > 0 and stats["checked"] == 0:
            self.stdout.write(self.style.ERROR(summary))
        elif stats["failed"] > 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    async def _run(self, states: Optional[list], dry_run: bool) -> dict:
        async with MedicaidWorkRequirementFetcher() as fetcher:
            return await fetcher.check_all(states=states, dry_run=dry_run)

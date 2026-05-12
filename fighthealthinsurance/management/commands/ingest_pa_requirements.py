"""
Fetch and re-parse carrier-published PA requirement lists.

Idempotent: replaces each carrier's auto-ingested
:class:`PayerPriorAuthRequirement` rows (those whose ``source_document``
starts with ``"auto:"``) with freshly parsed entries from the live URL.
Manually seeded rows are left untouched. Rows that disappear from the
upstream list are soft-retired with ``end_date=today`` so historical
denials can still resolve to the rule that applied at the time.

Usage::

    python manage.py ingest_pa_requirements
    python manage.py ingest_pa_requirements --company "UnitedHealthcare"
    python manage.py ingest_pa_requirements --company "UHC" --dry-run
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand

from fighthealthinsurance.models import InsuranceCompany
from fighthealthinsurance.pa_requirements_fetcher import PaRequirementsFetcher


class Command(BaseCommand):
    help = "Fetch and parse every payer prior-authorization requirement list."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            metavar="NAME",
            help=(
                "Restrict ingestion to a single company by exact name (case-"
                "insensitive). Default: every company flagged "
                "pa_requirement_list_url_is_parseable=True."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Fetch + parse but do NOT write to the database. Useful for "
                "previewing what a refresh would change."
            ),
        )

    def handle(self, *args: str, **options: Any) -> None:
        company_name: Optional[str] = options.get("company")
        dry_run: bool = bool(options.get("dry_run"))

        if dry_run:
            stats = asyncio.run(self._dry_run(company_name))
            summary = (
                f"[DRY RUN] Would have processed {stats['fetched']} carrier(s); "
                f"{stats['failed']} failed; parsed {stats['entries']} "
                f"requirement record(s)."
            )
        else:
            stats = asyncio.run(self._ingest(company_name))
            summary = (
                f"PA requirements ingest: {stats['fetched']} carrier(s) fetched, "
                f"{stats['failed']} failed, {stats['entries']} rows upserted, "
                f"{stats.get('retired', 0)} rows soft-retired."
            )

        # Mirror ingest_payer_policy_indexes' exit-zero semantics with
        # WARNING/ERROR styling on partial/total failure.
        if stats["failed"] > 0 and stats["fetched"] == 0:
            self.stdout.write(self.style.ERROR(summary))
        elif stats["failed"] > 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    async def _ingest(self, company_name: Optional[str]) -> dict:
        async with PaRequirementsFetcher() as fetcher:
            if company_name:
                company = await self._resolve_company(company_name)
                if company is None:
                    return {"fetched": 0, "failed": 1, "entries": 0, "retired": 0}
                return await fetcher.ingest_company(company)
            return await fetcher.ingest_all()

    async def _dry_run(self, company_name: Optional[str]) -> dict:
        stats = {"fetched": 0, "failed": 0, "entries": 0}
        companies: List[InsuranceCompany]
        if company_name:
            company = await self._resolve_company(company_name)
            companies = [company] if company is not None else []
            if company is None:
                stats["failed"] += 1
        else:
            companies = await sync_to_async(
                lambda: list(
                    InsuranceCompany.objects.filter(
                        pa_requirement_list_url_is_parseable=True,
                    ).exclude(pa_requirement_list_url="")
                )
            )()

        async with PaRequirementsFetcher() as fetcher:
            for company in companies:
                try:
                    parsed = await fetcher.parse_company(company)
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f"  [{company.name}] parse failed: {e}")
                    )
                    stats["failed"] += 1
                    continue
                stats["fetched"] += 1
                stats["entries"] += len(parsed)
                self.stdout.write(
                    f"  [{company.name}] would write {len(parsed)} row(s) "
                    f"from {company.pa_requirement_list_url}"
                )
        return stats

    @staticmethod
    async def _resolve_company(name: str) -> Optional[InsuranceCompany]:
        return await sync_to_async(
            lambda: InsuranceCompany.objects.filter(name__iexact=name).first()
        )()

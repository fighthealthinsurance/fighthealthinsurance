"""
Fetch and re-parse every payer prior-authorization requirement list.

Idempotent: replaces each company's auto-ingested
:class:`PayerPriorAuthRequirement` rows (those whose ``source_document``
starts with ``"auto:"``) with freshly parsed entries from the live URL.
Manually seeded rows are left untouched.

Usage::

    python manage.py ingest_pa_requirements
    python manage.py ingest_pa_requirements --company "UnitedHealthcare"
    python manage.py ingest_pa_requirements --company "UHC" --dry-run
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand

from fighthealthinsurance.pa_requirement_fetcher import PARequirementFetcher


class Command(BaseCommand):
    help = "Fetch and parse every payer prior-authorization requirement list."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            metavar="NAME",
            help="Restrict ingestion to a single company by exact name.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Fetch and parse documents but do not write to the database.",
        )

    def handle(self, *args: str, **options: Any) -> None:
        company_name: Optional[str] = options.get("company")
        dry_run: bool = options.get("dry_run", False)

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes."))

        stats = asyncio.run(self._run(company_name, dry_run))

        if stats.get("not_found"):
            self.stdout.write(
                self.style.ERROR(f"Company {company_name!r} not found in the database.")
            )
            return

        summary = (
            f"PA requirement ingest: {stats['fetched']} payer(s) processed, "
            f"{stats['failed']} failed, {stats['entries']} requirement(s) "
            f"{'parsed' if dry_run else 'written'}."
        )
        if stats["failed"] > 0 and stats["fetched"] == 0:
            self.stdout.write(self.style.ERROR(summary))
        elif stats["failed"] > 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    async def _run(self, company_name: Optional[str], dry_run: bool) -> Dict[str, int]:
        async with PARequirementFetcher() as fetcher:
            if company_name is None:
                if dry_run:
                    return await self._dry_run_all(fetcher)
                return await fetcher.ingest_all()

            from fighthealthinsurance.models import InsuranceCompany

            company = await sync_to_async(
                InsuranceCompany.objects.filter(name__iexact=company_name).first
            )()
            if company is None:
                return {"fetched": 0, "failed": 0, "entries": 0, "not_found": 1}
            if not company.pa_requirement_list_url_is_parseable:
                self.stdout.write(
                    self.style.WARNING(
                        f"{company.name} has pa_requirement_list_url_is_parseable=False; "
                        "set it True to enable auto-ingestion."
                    )
                )
                return {"fetched": 0, "failed": 0, "entries": 0}

            if dry_run:
                return await self._dry_run_company(fetcher, company)
            count = await fetcher.ingest_company(company)
            return {"fetched": 1, "failed": 0, "entries": count}

    async def _dry_run_company(self, fetcher, company) -> Dict[str, int]:
        try:
            reqs = await fetcher.parse_company(company)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Parse failed for {company.name}: {e}"))
            return {"fetched": 0, "failed": 1, "entries": 0}

        self.stdout.write(f"[dry-run] {company.name}: parsed {len(reqs)} requirement(s)")
        for r in reqs[:5]:
            label = r.cpt_hcpcs_code or f"{r.code_range_start}-{r.code_range_end}"
            self.stdout.write(f"  {label}: {r.code_description[:60]}")
        if len(reqs) > 5:
            self.stdout.write(f"  … and {len(reqs) - 5} more")
        return {"fetched": 1, "failed": 0, "entries": len(reqs)}

    async def _dry_run_all(self, fetcher) -> Dict[str, int]:
        from fighthealthinsurance.models import InsuranceCompany

        companies = await sync_to_async(
            lambda: list(
                InsuranceCompany.objects.filter(
                    pa_requirement_list_url_is_parseable=True,
                ).exclude(pa_requirement_list_url="")
            )
        )()
        totals = {"fetched": 0, "failed": 0, "entries": 0}
        for company in companies:
            partial = await self._dry_run_company(fetcher, company)
            for k in totals:
                totals[k] += partial.get(k, 0)
        return totals

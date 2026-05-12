"""
Fetch and re-parse every payer prior-authorization requirement list.

Idempotent: replaces each company's auto-ingested
:class:`PayerPriorAuthRequirement` rows (those whose ``source_document``
starts with ``"auto:"``) with freshly parsed entries from the live URL.
Manually seeded rows are left untouched.

Intended to run at deploy time alongside ``ingest_payer_policy_indexes`` and
on a periodic schedule so PA requirement lookups stay current.

Usage::

    python manage.py ingest_pa_requirements
    python manage.py ingest_pa_requirements --company "UnitedHealthcare"
    python manage.py ingest_pa_requirements --dry-run
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

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
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes will occur."))

        async def _run() -> dict[str, int]:
            async with PARequirementFetcher() as fetcher:
                if company_name:
                    from asgiref.sync import sync_to_async
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
                                "set it to True to enable auto-ingestion."
                            )
                        )
                        return {"fetched": 0, "failed": 0, "entries": 0}
                    if dry_run:
                        # Parse only — skip the DB write
                        from fighthealthinsurance.pa_requirement_fetcher import (
                            AUTO_SOURCE_PREFIX,
                        )
                        from urllib.parse import urlparse

                        from fighthealthinsurance.pa_requirement_parsers import (
                            PARSERS_BY_HOST,
                            PARSERS_BY_CONTENT_TYPE,
                        )

                        url = company.pa_requirement_list_url
                        host = (urlparse(url).hostname or "").lower()
                        parser_spec = PARSERS_BY_HOST.get(host)
                        content_type, raw = await fetcher._get_content(url)
                        if parser_spec:
                            parser_fn, is_binary = parser_spec
                        else:
                            ct_key = (content_type or "").split(";")[0].strip().lower()
                            parser_fn = PARSERS_BY_CONTENT_TYPE.get(ct_key)
                            is_binary = True
                        if parser_fn is None:
                            return {"fetched": 0, "failed": 1, "entries": 0}
                        if is_binary:
                            reqs = parser_fn(raw, f"{AUTO_SOURCE_PREFIX}{url}")
                        else:
                            text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                            reqs = parser_fn(text, f"{AUTO_SOURCE_PREFIX}{url}")
                        self.stdout.write(
                            f"[dry-run] Would write {len(reqs)} requirements for {company.name}"
                        )
                        for r in reqs[:5]:
                            self.stdout.write(f"  {r.cpt_hcpcs_code}: {r.code_description[:60]}")
                        if len(reqs) > 5:
                            self.stdout.write(f"  … and {len(reqs) - 5} more")
                        return {"fetched": 1, "failed": 0, "entries": len(reqs)}
                    count = await fetcher.ingest_company(company)
                    return {"fetched": 1, "failed": 0, "entries": count}
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING("--dry-run with no --company fetches all payers but writes nothing.")
                    )
                return await fetcher.ingest_all()

        stats = asyncio.run(_run())

        if stats.get("not_found"):
            self.stdout.write(
                self.style.ERROR(f"Company {company_name!r} not found in the database.")
            )
            return

        summary = (
            f"PA requirement ingest: {stats['fetched']} payer(s) fetched, "
            f"{stats['failed']} failed, {stats['entries']} requirements written."
        )
        if stats["failed"] > 0 and stats["fetched"] == 0:
            self.stdout.write(self.style.ERROR(summary))
        elif stats["failed"] > 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

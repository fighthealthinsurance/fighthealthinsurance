"""Ingest the monthly worst-insurance-by-state rankings artifact.

Fetches the pipeline repo's published ``rankings.json`` (or reads a local
copy) and idempotently upserts the report + ranking rows. Pass ``--url`` or
``--file``; idempotent on ``(period)`` / ``(report, state, issuer_slug)``.
"""

import json
from typing import Any, Optional

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.worst_insurance_ingest import fetch_json, load_report_dict


class Command(BaseCommand):
    help = "Ingest worst-insurance-by-state rankings JSON from a URL or file."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--url",
            type=str,
            help="URL to rankings.json (mutually exclusive with --file).",
        )
        parser.add_argument(
            "--file",
            type=str,
            help="Local path to a previously-downloaded rankings.json.",
        )

    def handle(self, *args: str, **options: Any) -> None:
        url: Optional[str] = options.get("url")
        path: Optional[str] = options.get("file")
        if not url and not path:
            raise CommandError("Provide --url or --file")
        if url and path:
            raise CommandError("Provide --url or --file, not both")

        if url:
            self.stdout.write(f"Fetching rankings JSON from {url}")
            data = fetch_json(url)
            source_url = url
        else:
            assert path is not None  # narrowed by the checks above
            self.stdout.write(f"Loading rankings JSON from {path}")
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            source_url = ""

        try:
            created, updated, deleted, match_failures = load_report_dict(
                data, source_url=source_url
            )
        except ValueError as e:
            raise CommandError(str(e))

        style = self.style.WARNING if match_failures else self.style.SUCCESS
        self.stdout.write(
            style(
                f"Worst-insurance ingest complete: period {data.get('period')}, "
                f"{created} created, {updated} updated, {deleted} deleted, "
                f"{match_failures} issuers without an InsuranceCompany match"
            )
        )

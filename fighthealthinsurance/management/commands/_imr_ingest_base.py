"""Shared base command for IMR / external-appeal CSV ingestion."""

from typing import Any, Optional

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.imr_ingest import fetch_csv, load_csv_text


class IMRIngestCommand(BaseCommand):
    """Base management command: fetch a CSV (URL or file) and upsert IMR rows.

    Subclasses set ``source`` to one of ``IMRDecision.SOURCE_*`` and
    ``label`` to a short human-readable name used in status messages.
    """

    source: str = ""
    label: str = ""

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--url",
            type=str,
            help="URL to the CSV (mutually exclusive with --file).",
        )
        parser.add_argument(
            "--file",
            type=str,
            help="Local path to a previously-downloaded CSV.",
        )

    def handle(self, *args: str, **options: Any) -> None:
        if not self.source:
            raise CommandError("Subclass must set `source`")
        url: Optional[str] = options.get("url")
        path: Optional[str] = options.get("file")
        if not url and not path:
            raise CommandError("Provide --url or --file")
        if url and path:
            raise CommandError("Provide --url or --file, not both")

        if url:
            self.stdout.write(f"Fetching {self.label} CSV from {url}")
            csv_text = fetch_csv(url)
            source_url = url
        else:
            assert path is not None  # narrowed by the checks above
            self.stdout.write(f"Loading {self.label} CSV from {path}")
            with open(path, "r", encoding="utf-8-sig") as fh:
                csv_text = fh.read()
            source_url = ""

        created, updated, skipped, failed = load_csv_text(
            csv_text, source=self.source, source_url=source_url
        )
        style = self.style.WARNING if failed else self.style.SUCCESS
        self.stdout.write(
            style(
                f"{self.label} ingest complete: "
                f"{created} created, {updated} updated, "
                f"{skipped} skipped, {failed} failed"
            )
        )

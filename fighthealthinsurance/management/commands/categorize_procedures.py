"""Backfill the broad categories of denied procedures for the staff chart.

Two LLM passes (see procedure_categories.py): discover a category list from
the most-denied procedures, then sort the uncategorized ones into it. Only
internal models are asked. Safe to re-run: it picks up where it left off
and keeps the categories it already created.
"""

from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from fighthealthinsurance import procedure_categories


class Command(BaseCommand):
    help = "Categorize denied procedures with an internal LLM (two passes)"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--recategorize",
            action="store_true",
            help="Start the category list fresh and re-sort every procedure",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Sort at most this many procedures (most-denied first)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Ask the model and print the result, but write nothing",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        stats = async_to_sync(procedure_categories.categorize_procedures)(
            recategorize=options["recategorize"],
            limit=options["limit"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            f"{stats['procedures']} distinct procedures, "
            f"{stats['to_categorize']} to categorize, "
            f"{stats['categorized']} categorized"
            + (" (dry run, nothing written)" if options["dry_run"] else "")
        )
        self.stdout.write("Categories: " + ", ".join(stats["categories"]))
        if options["dry_run"]:
            for procedure, category in stats.get("assignments", {}).items():
                self.stdout.write(f"  {category}: {procedure}")

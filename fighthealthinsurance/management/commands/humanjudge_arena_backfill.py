"""Write every arena model's missing answers to the HumanJudge arena cases.

The admin's export starts this in a background thread on a web pod, which
a restart cuts short; this runs the same backfill (humanjudge_arena.backfill)
to completion from a shell, so HumanJudge's calls (which wait 60 seconds
each) find the answers stored. Answers already stored are left as they are.
"""

from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from fighthealthinsurance import humanjudge_arena


class Command(BaseCommand):
    help = "Write the HumanJudge arena answers its cases still lack"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--case",
            type=int,
            action="append",
            dest="case_ids",
            help="Only this arena case id (repeatable; default: every case)",
        )
        parser.add_argument(
            "--model",
            action="append",
            dest="model_names",
            help="Only this model (repeatable; default: every arena model)",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=humanjudge_arena.BACKFILL_CONCURRENCY,
            help="Letters written at once",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        result = async_to_sync(humanjudge_arena.backfill)(
            options["case_ids"],
            options["model_names"],
            options["concurrency"],
        )
        self.stdout.write(
            f"Already stored: {result.already}; Arena drafts: "
            f"{result.arena_drafts}; written now: {result.generated}; "
            f"failed: {result.failed}"
        )

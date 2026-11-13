"""Settle the Appeal Arena's tasks whose votes already say enough.

A task is checked after each vote and skip (chooser_settlement.settle_task);
this applies the same rules to the whole pool, for tasks voted on before
settling existed, or after the settings that drive it change. With
``--reopen``, a settled task the rules no longer settle goes back to READY,
so loosening a threshold can be undone without touching the database by
hand.
"""

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from fighthealthinsurance import chooser_settlement
from fighthealthinsurance.models import ChooserTask


class Command(BaseCommand):
    help = (
        "Stop serving chooser tasks whose votes show a clear winner or that "
        "are clearly undecided (and, with --reopen, serve again settled tasks "
        "the rules no longer settle)"
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--task-type",
            choices=[choice for choice, _ in ChooserTask.TASK_TYPE_CHOICES],
            default=None,
            help="Only tasks of this type (default: both)",
        )
        parser.add_argument(
            "--reopen",
            action="store_true",
            help="Also put settled tasks the rules no longer settle back to READY",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without storing it",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = options["dry_run"]
        verbose = int(options.get("verbosity", 1)) >= 2
        wanted = Q(status="READY")
        if options["reopen"]:
            wanted |= Q(status="EXHAUSTED") & ~Q(settled_reason="")
        tasks = ChooserTask.objects.filter(wanted)
        if options["task_type"]:
            tasks = tasks.filter(task_type=options["task_type"])
        counts: Counter = Counter()
        for task_id, status, reason in (
            tasks.order_by("id")
            .values_list("id", "status", "settled_reason")
            .iterator()
        ):
            found = chooser_settlement.task_verdict(task_id)
            if status == "READY":
                if found is None:
                    continue
                counts[found.reason] += 1
                if verbose:
                    self.stdout.write(
                        f"task {task_id}: settle ({found.reason}): {found.detail}"
                    )
                if not dry_run:
                    ChooserTask.objects.filter(id=task_id, status="READY").update(
                        status="EXHAUSTED",
                        settled_reason=found.reason,
                        settled_at=timezone.now(),
                    )
            elif found is None:
                counts["reopened"] += 1
                if verbose:
                    self.stdout.write(f"task {task_id}: reopen (was {reason})")
                if not dry_run:
                    ChooserTask.objects.filter(id=task_id, status="EXHAUSTED").update(
                        status="READY", settled_reason="", settled_at=None
                    )
            elif found.reason != reason:
                counts[f"{reason} -> {found.reason}"] += 1
                if not dry_run:
                    ChooserTask.objects.filter(id=task_id).update(
                        settled_reason=found.reason
                    )
        verb = "Would settle" if dry_run else "Settled"
        self.stdout.write(
            f"{verb} {counts[chooser_settlement.CLEAR_WINNER]} task(s) with a clear "
            f"winner and {counts[chooser_settlement.UNDECIDED]} clearly undecided"
            + (
                f"; {'would reopen' if dry_run else 'reopened'} {counts['reopened']}"
                if options["reopen"]
                else ""
            )
            + (" (dry run, nothing stored)" if dry_run else "")
        )

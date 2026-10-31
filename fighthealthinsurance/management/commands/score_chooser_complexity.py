"""Score, or re-score, the Appeal Arena's tasks for judging complexity.

A new task is scored as it goes READY (chooser_tasks._generate_single_task);
this fills in the tasks from before the score existed, which the next-task
ordering meanwhile serves as the hardest. The score is deterministic
(chooser_complexity.score_task), so ``--rescore`` after a change to the
rubric brings every task up to date. Nothing is scored on deploy: the
unscored tasks stay unscored until this is run.
"""

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance import chooser_complexity
from fighthealthinsurance.models import ChooserTask


class Command(BaseCommand):
    help = (
        "Score READY chooser tasks for judging complexity "
        "(the unscored ones, unless --rescore)"
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--task-type",
            choices=[choice for choice, _ in ChooserTask.TASK_TYPE_CHOICES],
            default=None,
            help="Only tasks of this type (default: both)",
        )
        parser.add_argument(
            "--rescore",
            action="store_true",
            help="Score every READY task, not only the ones without a score",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the scores without storing them",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        tasks = ChooserTask.objects.filter(status="READY")
        if options["task_type"]:
            tasks = tasks.filter(task_type=options["task_type"])
        if not options["rescore"]:
            tasks = tasks.filter(complexity__isnull=True)
        verbose = int(options.get("verbosity", 1)) >= 2
        histogram: Counter = Counter()
        changed = 0
        for task in tasks.order_by("id").iterator():
            detail = chooser_complexity.breakdown_for(task)
            histogram[detail.score] += 1
            if verbose:
                self.stdout.write(
                    f"task {task.id} ({task.task_type}): {detail.score} "
                    f"({detail.words} words in {detail.drafts} drafts and the case, "
                    f"{detail.articles} articles, {detail.citations} citations, "
                    f"~{detail.minutes:.1f} minutes)"
                )
            if task.complexity == detail.score:
                continue
            changed += 1
            if not options["dry_run"]:
                # update(), not save(): a score is not an edit of the task,
                # and need not touch updated_at.
                ChooserTask.objects.filter(pk=task.pk).update(complexity=detail.score)
        total = sum(histogram.values())
        verb = "Would score" if options["dry_run"] else "Scored"
        self.stdout.write(
            f"{verb} {total} task(s); {changed} with a new score"
            + (" (dry run, nothing stored)" if options["dry_run"] else "")
        )
        for level in range(
            chooser_complexity.COMPLEXITY_MIN, chooser_complexity.COMPLEXITY_MAX + 1
        ):
            self.stdout.write(f"  complexity {level}: {histogram.get(level, 0)}")

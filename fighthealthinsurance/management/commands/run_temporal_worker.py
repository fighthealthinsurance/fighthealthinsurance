"""Run the Temporal worker that hosts FHI workflows and activities.

This is the Temporal analogue of the Ray actor processes: a long-running worker
that polls a task queue and executes ``SendFaxWorkflow`` plus its fax activities.
Activities are synchronous (blocking ORM + vendor I/O), so they run in a
``ThreadPoolExecutor``.

Run it as its own process / Kubernetes Deployment::

    python manage.py run_temporal_worker
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Temporal worker for FHI workflows and activities."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--task-queue",
            default=None,
            help="Override the task queue (defaults to settings.TEMPORAL_TASK_QUEUE).",
        )
        parser.add_argument(
            "--max-workers",
            type=int,
            default=None,
            help=(
                "Max activity threads (defaults to "
                "settings.TEMPORAL_MAX_ACTIVITY_WORKERS)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        asyncio.run(self._run(options))

    async def _run(self, options: dict) -> None:
        from temporalio.worker import Worker

        from fighthealthinsurance.activities import (
            appeal_journey as journey_activities,
            fax as fax_activities,
        )
        from fighthealthinsurance.temporal_client import get_temporal_client
        from fighthealthinsurance.workflows.generate_appeal import (
            GenerateAppealWorkflow,
        )
        from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow

        task_queue = options.get("task_queue") or settings.TEMPORAL_TASK_QUEUE
        max_workers = options.get("max_workers") or getattr(
            settings, "TEMPORAL_MAX_ACTIVITY_WORKERS", 20
        )

        from typing import Any as _Any, Callable, List

        fax_workflows: List[type] = [SendFaxWorkflow]
        fax_activity_fns: List[Callable[..., _Any]] = [
            fax_activities.precheck_fax,
            fax_activities.send_fax_via_vendor,
            fax_activities.release_send_claim,
            fax_activities.finalize_fax,
        ]
        # Register the appeal journey only when its flag is on, so the flag
        # is a real execution kill switch: with unconditional registration a
        # direct Temporal start (or a task queued before the flag flipped)
        # would still run on a "dark" worker (PR #963 review).
        journey_enabled = getattr(settings, "TEMPORAL_APPEAL_JOURNEY_ENABLED", False)

        client = await get_temporal_client()
        self.stdout.write(
            f"Connected to Temporal at {settings.TEMPORAL_HOST} "
            f"(namespace={settings.TEMPORAL_NAMESPACE}); appeal journey "
            f"{'ENABLED' if journey_enabled else 'disabled'}"
        )

        with ThreadPoolExecutor(max_workers=max_workers) as activity_executor:
            fax_worker = Worker(
                client,
                task_queue=task_queue,
                workflows=fax_workflows,
                activities=fax_activity_fns,
                activity_executor=activity_executor,
            )
            runs = [fax_worker.run()]
            queues = [task_queue]
            if journey_enabled:
                # The journey runs on its OWN task queue and worker: several
                # slow appeal generations must never occupy the fax worker's
                # activity slots (separate failure domain; PR #963 review).
                # Its activities are asyncio activities, so it needs no
                # thread executor and its concurrency is bounded separately.
                appeal_queue = settings.TEMPORAL_APPEAL_TASK_QUEUE
                appeal_worker = Worker(
                    client,
                    task_queue=appeal_queue,
                    workflows=[GenerateAppealWorkflow],
                    activities=[
                        journey_activities.precheck_appeal_journey,
                        journey_activities.generate_and_store_appeals,
                    ],
                    max_concurrent_activities=4,
                )
                runs.append(appeal_worker.run())
                queues.append(appeal_queue)
            self.stdout.write(
                f"Starting Temporal worker(s) on task queue(s) "
                f"{', '.join(repr(q) for q in queues)} "
                f"({max_workers} fax activity threads). Ctrl-C to stop."
            )
            await asyncio.gather(*runs)

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

        from fighthealthinsurance.activities import fax as fax_activities
        from fighthealthinsurance.temporal_client import get_temporal_client
        from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow

        task_queue = options.get("task_queue") or settings.TEMPORAL_TASK_QUEUE
        max_workers = options.get("max_workers") or getattr(
            settings, "TEMPORAL_MAX_ACTIVITY_WORKERS", 20
        )

        client = await get_temporal_client()
        self.stdout.write(
            f"Connected to Temporal at {settings.TEMPORAL_HOST} "
            f"(namespace={settings.TEMPORAL_NAMESPACE})"
        )

        with ThreadPoolExecutor(max_workers=max_workers) as activity_executor:
            worker = Worker(
                client,
                task_queue=task_queue,
                workflows=[SendFaxWorkflow],
                activities=[
                    fax_activities.precheck_fax,
                    fax_activities.send_fax_via_vendor,
                    fax_activities.finalize_fax,
                ],
                activity_executor=activity_executor,
            )
            self.stdout.write(
                f"Starting Temporal worker on task queue '{task_queue}' "
                f"({max_workers} activity threads). Ctrl-C to stop."
            )
            await worker.run()

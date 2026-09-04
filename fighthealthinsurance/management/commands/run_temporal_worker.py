"""Run the Temporal worker that hosts FHI workflows and activities.

This is the Temporal analogue of the Ray actor processes: a long-running worker
that polls a task queue and executes ``SendFaxWorkflow`` plus its fax activities.
Activities are synchronous (blocking ORM + vendor I/O), so they run in a
``ThreadPoolExecutor``.

Run it as its own process / Kubernetes Deployment::

    python manage.py run_temporal_worker

``--queues`` (or ``TEMPORAL_WORKER_QUEUES``) selects which queue(s) this
process hosts, so fax and appeal work can run as SEPARATE Deployments and
share no failure domain: appeal-generation memory/CPU pressure or an appeal
crash-loop must never take fax sending down with it (external review).
``all`` (the default) keeps the single-process shape for dev and small
installs.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

QUEUE_ROLES = ("fax", "appeal", "all")

# Prometheus scrape endpoint for the SDK's worker metrics. Temporal accepts
# work for a task queue with no healthy poller and queues it silently, so
# worker health must be OBSERVED: schedule-to-start latency, task-slot
# availability, activity failures, RPC failures (external review). Set by
# the worker manifests; unset (dev, tests) means no endpoint.
METRICS_BIND_ENV = "TEMPORAL_METRICS_BIND"


def metrics_runtime() -> Any:
    """The SDK ``Runtime`` carrying the Prometheus config, or None when
    ``TEMPORAL_METRICS_BIND`` is unset/blank."""
    bind = (os.environ.get(METRICS_BIND_ENV) or "").strip()
    if not bind:
        return None
    from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

    return Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(
                bind_address=bind,
                # Seconds, not the SDK's millisecond default, so alert
                # thresholds in k8s/temporal/worker-alerts.yaml read plainly.
                durations_as_seconds=True,
            )
        )
    )


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
        parser.add_argument(
            "--queues",
            choices=QUEUE_ROLES,
            default=None,
            help=(
                "Which queue(s) this process hosts: 'fax', 'appeal', or "
                "'all'. Defaults to TEMPORAL_WORKER_QUEUES, else 'all'."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        asyncio.run(self._run(options))

    async def _run(self, options: dict) -> None:
        from temporalio.worker import Worker

        from fighthealthinsurance.activities import (
            appeal_journey as journey_activities,
            fax as fax_activities,
            intake_journey as intake_activities,
        )
        from fighthealthinsurance.temporal_client import get_temporal_client
        from fighthealthinsurance.workflows.generate_appeal import (
            GenerateAppealWorkflow,
        )
        from fighthealthinsurance.workflows.intake_journey import (
            IntakeJourneyWorkflow,
        )
        from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow

        max_workers = options.get("max_workers") or getattr(
            settings, "TEMPORAL_MAX_ACTIVITY_WORKERS", 20
        )
        role = (
            options.get("queues") or os.environ.get("TEMPORAL_WORKER_QUEUES") or "all"
        ).lower()
        if role not in QUEUE_ROLES:
            raise CommandError(
                f"TEMPORAL_WORKER_QUEUES={role!r}: expected one of {QUEUE_ROLES}"
            )
        # --task-queue overrides the queue of the SELECTED role: for the fax
        # role (or 'all') it replaces the fax queue as before; for the appeal
        # role it replaces the appeal queue, instead of silently setting a fax
        # queue that role never polls (review).
        queue_override = options.get("task_queue")
        task_queue = (
            queue_override if role != "appeal" and queue_override else None
        ) or settings.TEMPORAL_TASK_QUEUE
        appeal_queue = (
            queue_override if role == "appeal" and queue_override else None
        ) or settings.TEMPORAL_APPEAL_TASK_QUEUE

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
        journey_enabled = getattr(settings, "TEMPORAL_ENABLED", False) and getattr(
            settings, "TEMPORAL_APPEAL_JOURNEY_ENABLED", False
        )

        runtime = metrics_runtime()
        client = await get_temporal_client(runtime=runtime)
        self.stdout.write(
            f"Connected to Temporal at {settings.TEMPORAL_HOST} "
            f"(namespace={settings.TEMPORAL_NAMESPACE}); role={role}; appeal "
            f"journey {'ENABLED' if journey_enabled else 'disabled'}; metrics "
            f"{os.environ.get(METRICS_BIND_ENV) if runtime else 'off'}"
        )

        with ThreadPoolExecutor(max_workers=max_workers) as activity_executor:
            runs = []
            queues = []
            if role in ("fax", "all"):
                fax_worker = Worker(
                    client,
                    task_queue=task_queue,
                    workflows=fax_workflows,
                    activities=fax_activity_fns,
                    activity_executor=activity_executor,
                    # Match the slot count to the thread executor: with more
                    # slots than threads, accepted activities queue locally
                    # while their start-to-close clock runs (review).
                    max_concurrent_activities=max_workers,
                    # Kubernetes sends SIGTERM at pod shutdown; a running
                    # send_fax_via_vendor attempt may legitimately spend up
                    # to its 30-minute start_to_close in vendor backends
                    # (~1300s each), and killing it mid-send risks the
                    # vendor delivering a fax whose result we lost -- the
                    # exact double-send the fax rule forbids. Drain covers
                    # the full activity bound; when nothing is running,
                    # shutdown is immediate, so rollouts only wait when a
                    # fax is actually in flight (review).
                    # terminationGracePeriodSeconds in the manifest must
                    # exceed this.
                    graceful_shutdown_timeout=timedelta(minutes=30),
                )
                runs.append(fax_worker.run())
                queues.append(task_queue)
            if role in ("appeal", "all") and journey_enabled:
                # The journey runs on its OWN task queue and worker: several
                # slow appeal generations must never occupy the fax worker's
                # activity slots (separate failure domain; PR #963 review).
                # Its activities are asyncio activities, so it needs no
                # thread executor and its concurrency is bounded separately
                # (low and explicit: current letter volume is small, and a
                # small bound is most of the blast-radius story).
                appeal_workflows: List[type] = [GenerateAppealWorkflow]
                appeal_activity_fns = [
                    journey_activities.precheck_appeal_journey,
                    journey_activities.generate_and_store_appeals,
                ]
                if getattr(settings, "TEMPORAL_INTAKE_JOURNEY_ENABLED", False):
                    appeal_workflows.append(IntakeJourneyWorkflow)
                    appeal_activity_fns += [
                        intake_activities.send_abandonment_nudge,
                        intake_activities.close_incomplete_journey,
                        intake_activities.check_generation_postcondition,
                    ]
                appeal_worker = Worker(
                    client,
                    task_queue=appeal_queue,
                    workflows=appeal_workflows,
                    activities=appeal_activity_fns,
                    max_concurrent_activities=4,
                    # Longer than the fax worker's: a generation attempt owns
                    # a GENERATION_BUDGET_SECONDS (240s) model-call window and
                    # cancelling it cooperatively takes time to drain.
                    graceful_shutdown_timeout=timedelta(seconds=300),
                )
                runs.append(appeal_worker.run())
                queues.append(appeal_queue)
            if not runs:
                # role=appeal with the journey flags dark. Idle instead of
                # exiting: an exit would crash-loop the Deployment, but this
                # pod being inert IS the kill switch working -- flipping the
                # flags and restarting the Deployment brings it live.
                self.stdout.write(
                    "Appeal worker role selected but the appeal journey flags "
                    "are OFF; idling (this process will host nothing until "
                    "TEMPORAL_ENABLED and TEMPORAL_APPEAL_JOURNEY_ENABLED are "
                    "set and the process restarts)."
                )
                await asyncio.Event().wait()
                return
            self.stdout.write(
                f"Starting Temporal worker(s) on task queue(s) "
                f"{', '.join(repr(q) for q in queues)} "
                f"({max_workers} fax activity threads). Ctrl-C to stop."
            )
            await asyncio.gather(*runs)

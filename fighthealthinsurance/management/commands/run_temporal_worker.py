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

The appeal role (and ``all``) also hosts the chat routing policy queue as a
Worker of its own when TEMPORAL_CHAT_POLICY_ENABLED is on, and keeps the
``chat-routing-policy`` Schedule in step with that flag at start-up.
"""

import asyncio
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from loguru import logger

from fighthealthinsurance.worker_signals import early_stop

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


# Scrape endpoint for the APP's own Prometheus registry: the fhi_ml_* call
# counters (ml/ml_metrics.py) and the DB pool gauges. Appeal generation that
# runs on this worker records into prometheus_client's default registry,
# which only the web pods served, so a backend failing only under the
# journey's calls showed no failures anywhere. A second port, because the
# SDK's Rust exporter owns the first. Set by the worker manifests; unset
# (dev, tests) means no endpoint.
APP_METRICS_BIND_ENV = "FHI_APP_METRICS_BIND"

# Upper bound on keeping the chat policy Schedule in step at start-up. The
# Schedule is housekeeping: a slow or failing Temporal call here must not
# keep this process from hosting its queues.
SCHEDULE_ENSURE_TIMEOUT_SECONDS = 30.0


def app_metrics_server() -> str | None:
    """Serve prometheus_client's registry on ``FHI_APP_METRICS_BIND``
    (``host:port``). Returns the bind, or None when unset/blank."""
    bind = (os.environ.get(APP_METRICS_BIND_ENV) or "").strip()
    if not bind:
        return None
    from prometheus_client import start_http_server

    try:
        host, _, port = bind.rpartition(":")
        start_http_server(int(port), addr=host or "0.0.0.0")
    except Exception:
        # A metrics problem (a malformed bind, a port a sidecar already
        # holds) must not keep the worker from hosting: generating appeals
        # matters more than having their counters scraped.
        logger.opt(exception=True).warning(
            f"Not serving app metrics: could not bind {bind!r} "
            f"({APP_METRICS_BIND_ENV})"
        )
        return None
    return bind


def install_shutdown_handlers(workers, stop, log, tasks=None) -> None:
    """Make SIGTERM/SIGINT stop polling and drain, instead of being dropped.

    In the container this process is PID 1. A PID 1 with no handler for a
    signal never gets the kernel's default action, so Kubernetes' SIGTERM at
    rollout did nothing at all: the old worker kept polling its task queue at
    full rate for the whole terminationGracePeriodSeconds (verified
    2026-09-11: both old fax-worker pods were still listed as pollers fifteen
    minutes after SIGTERM, and a resend of fax 658 ran on the old image after
    the fix for it had been deployed). SIGINT was never affected, which is why
    Ctrl-C always looked fine locally.

    ``Worker.shutdown()`` stops polling and lets in-flight activities run
    for ``graceful_shutdown_timeout`` before asking them to cancel, then
    waits for them to actually finish. That is a healthy-path bound, not a
    hard one: a synchronous activity blocked in native or database I/O can
    ignore the cancellation and hold the process past the pod's grace
    period, at which point Kubernetes SIGKILLs it as before (review).
    ``stop`` is set first so the two phases that have no worker to shut
    down -- the client connect and the idle (no-worker) branch -- exit
    instead of sleeping through the grace period. Install this BEFORE the
    connect: ``workers`` is appended to later and is read at signal time.
    ``tasks`` keeps the shutdown task references alive (a bare
    ``create_task`` result can be garbage-collected mid-flight).

    Signal-driven shutdown is supported only when the command runs on the
    main thread of its process, which is how the container runs it (PID 1).
    Off the main thread neither this nor the bootstrap guard can install a
    handler; the command then runs, but only a loop cancellation stops it.
    """
    loop = asyncio.get_running_loop()

    def _request(signame: str) -> None:
        if stop.is_set():
            return
        stop.set()
        # Shutdown first, logging second: a log write can fail (BrokenPipe
        # once stdout's reader is gone) and must not stand between the
        # signal and the drain; a second signal is a no-op (review).
        for w in workers:
            task = loop.create_task(w.shutdown())
            if tasks is not None:
                tasks.append(task)
        try:
            log(f"{signame} received: stopping polling, draining in-flight activities")
        except Exception:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        if signal.getsignal(sig) is None:
            # Installed outside Python (embedded interpreter): the loop's
            # close could only put SIG_DFL back, so leave it alone (review).
            continue
        try:
            loop.add_signal_handler(sig, _request, sig.name)
        except (NotImplementedError, RuntimeError):
            # Not a Unix main-thread loop: unsupported for signal-driven
            # shutdown (see the docstring); leave the default disposition.
            pass


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
        # Belt and braces with manage.py: idempotent, shares the same flag.
        # The loop in _run replaces BOTH SIGTERM and SIGINT and asyncio puts
        # back the defaults on close, not what a library caller had, so save
        # and restore both here (review). Signal ownership is only possible
        # on the main thread; elsewhere there is nothing to restore.
        on_main_thread = threading.current_thread() is threading.main_thread()
        previous = (
            {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            if on_main_thread
            else {}
        )
        # The guard is only acquired and released on the main thread: a
        # guard preinstalled by manage.py must not be released (or fail to
        # be released, raising) from a worker thread (review).
        event = early_stop.install() if on_main_thread else early_stop.event
        try:
            asyncio.run(self._run(options, early_stop=event))
        finally:
            if on_main_thread:
                # Hands SIGTERM back to whatever preceded the bootstrap guard.
                early_stop.restore()
            for sig, handler in previous.items():
                if handler is None or handler == early_stop._handler:
                    # None: native, never ours. The guard's own handler: the
                    # restore() above already released it; reinstalling it
                    # here would resurrect a guard nobody owns (review).
                    continue
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

    async def _run(self, options: dict, early_stop=None) -> None:
        # Handlers first, before the lazy imports below: a SIGTERM that lands
        # during those imports would otherwise be discarded (PID 1), and the
        # process would go on to connect and poll on a stale image.
        workers: list = []
        shutdown_tasks: list = []
        stop = asyncio.Event()
        install_shutdown_handlers(workers, stop, self.stdout.write, shutdown_tasks)
        from fighthealthinsurance.ml import spend

        # Load the spend ledger at boot, not on the first activity.
        spend._ledger.start()
        if early_stop is not None and early_stop.is_set():
            # Caught by the bootstrap-time handler before the loop existed.
            stop.set()

        from temporalio.worker import Worker

        from fighthealthinsurance.activities import (
            appeal_journey as journey_activities,
            assistant_appeal as draft_activities,
            chat_routing_policy as policy_activities,
            fax as fax_activities,
            intake_journey as intake_activities,
        )
        from fighthealthinsurance.temporal_client import (
            chat_policy_schedule_enabled,
            ensure_chat_policy_schedule,
            get_temporal_client,
        )
        from fighthealthinsurance.workflows.generate_appeal import (
            GenerateAppealWorkflow,
        )
        from fighthealthinsurance.workflows.intake_journey import (
            IntakeJourneyWorkflow,
        )
        from fighthealthinsurance.workflows import registry as workflow_registry
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

        fax_workflows: List[type] = workflow_registry.fax_workflows()
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
        # The chat routing policy has its own flag and its own queue, hosted
        # by the appeal-worker process whatever the journey flags say. It is
        # registered only while the flag is on, for the same kill-switch
        # reason. --task-queue never changes this queue.
        policy_enabled = chat_policy_schedule_enabled()
        policy_queue = settings.TEMPORAL_CHAT_POLICY_TASK_QUEUE
        hosts_policy = role in ("appeal", "all")

        # The connect is raced against stop: a SIGTERM that lands while the
        # connection is still pending must not be discarded (review), and a
        # terminating pod must not start polling afterwards.
        runtime = metrics_runtime()
        app_metrics = app_metrics_server()
        connect = asyncio.ensure_future(get_temporal_client(runtime=runtime))
        stopped = asyncio.ensure_future(stop.wait())
        await asyncio.wait({connect, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            connect.cancel()
            self.stdout.write("Stop requested during startup; not hosting a worker.")
            return
        stopped.cancel()
        client = connect.result()
        self.stdout.write(
            f"Connected to Temporal at {settings.TEMPORAL_HOST} "
            f"(namespace={settings.TEMPORAL_NAMESPACE}); role={role}; appeal "
            f"journey {'ENABLED' if journey_enabled else 'disabled'}; chat "
            f"policy {'ENABLED' if policy_enabled and hosts_policy else 'disabled'}; "
            f"metrics {os.environ.get(METRICS_BIND_ENV) if runtime else 'off'}; "
            f"app metrics {app_metrics or 'off'}"
        )
        if hosts_policy:
            # Create or update the Schedule when the flag is on, pause it when
            # it is off. Only the process that hosts the policy queue does
            # this, so the Schedule never runs with nobody polling for it;
            # two replicas doing it at once is harmless.
            try:
                outcome = await asyncio.wait_for(
                    ensure_chat_policy_schedule(client, enabled=policy_enabled),
                    timeout=SCHEDULE_ENSURE_TIMEOUT_SECONDS,
                )
                self.stdout.write(f"Chat routing policy schedule: {outcome}")
            except Exception as e:
                # Class name only. Hosting goes on; the next start retries.
                logger.warning(
                    "Could not keep the chat routing policy schedule in step: "
                    f"{type(e).__name__}"
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
                workers.append(fax_worker)
                queues.append(task_queue)
            if role in ("appeal", "all") and journey_enabled:
                # The journey runs on its OWN task queue and worker: several
                # slow appeal generations must never occupy the fax worker's
                # activity slots (separate failure domain; PR #963 review).
                # Its activities are asyncio activities, so it needs no
                # thread executor and its concurrency is bounded separately
                # (low and explicit: current letter volume is small, and a
                # small bound is most of the blast-radius story).
                from fighthealthinsurance.assistant_drafts import draft_in_chat_enabled

                drafts_enabled = draft_in_chat_enabled()
                appeal_workflows: List[type] = workflow_registry.appeal_workflows(
                    intake_enabled=getattr(
                        settings, "TEMPORAL_INTAKE_JOURNEY_ENABLED", False
                    ),
                    drafts_enabled=drafts_enabled,
                )
                appeal_activity_fns: List[Callable[..., _Any]] = [
                    journey_activities.precheck_appeal_journey,
                    journey_activities.generate_and_store_appeals,
                ]
                intake_enabled = getattr(
                    settings, "TEMPORAL_INTAKE_JOURNEY_ENABLED", False
                )
                if drafts_enabled:
                    appeal_activity_fns += [
                        draft_activities.read_letter,
                        draft_activities.ask_questions,
                        draft_activities.start_drafting,
                        draft_activities.finish_drafts,
                        draft_activities.mark_draft_status,
                    ]
                if intake_enabled:
                    appeal_activity_fns += [
                        intake_activities.send_abandonment_nudge,
                        intake_activities.close_incomplete_journey,
                    ]
                if intake_enabled or drafts_enabled:
                    # Both journeys reconcile with it; registered once.
                    appeal_activity_fns.append(
                        journey_activities.check_generation_postcondition
                    )
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
                workers.append(appeal_worker)
                queues.append(appeal_queue)
            if hosts_policy and policy_enabled:
                # Its own Worker, so the policy run never waits for a slot
                # behind a long appeal generation. One activity at a time is
                # plenty for one run a day.
                policy_workflows: List[type] = workflow_registry.chat_policy_workflows()
                policy_worker = Worker(
                    client,
                    task_queue=policy_queue,
                    workflows=policy_workflows,
                    activities=[policy_activities.compute_and_store_chat_policy],
                    max_concurrent_activities=1,
                    # Covers the activity's two-minute start_to_close, inside
                    # the appeal worker's 300s drain and the pod's grace.
                    graceful_shutdown_timeout=timedelta(minutes=2),
                )
                runs.append(policy_worker.run())
                workers.append(policy_worker)
                queues.append(policy_queue)
            if not runs:
                # role=appeal with the journey and policy flags dark. Idle
                # instead of exiting: an exit would crash-loop the
                # Deployment, but this pod being inert IS the kill switch
                # working -- flipping the flags and restarting the
                # Deployment brings it live.
                self.stdout.write(
                    "Appeal worker role selected but the appeal journey and "
                    "chat policy flags are OFF; idling (this process will host "
                    "nothing until TEMPORAL_ENABLED and "
                    "TEMPORAL_APPEAL_JOURNEY_ENABLED or "
                    "TEMPORAL_CHAT_POLICY_ENABLED are set and the process "
                    "restarts)."
                )
                await stop.wait()
                return
            self.stdout.write(
                f"Starting Temporal worker(s) on task queue(s) "
                f"{', '.join(repr(q) for q in queues)} "
                f"({max_workers} fax activity threads). Ctrl-C to stop."
            )
            if stop.is_set():
                # Signalled at an await between the connect and here: nothing
                # has polled yet, so exit without starting the run loops. A
                # signal queued during the synchronous construction above is
                # delivered inside gather instead; polling may already have
                # started and accepted work by then (the SDK runs several
                # pollers), which the graceful drain is sized to finish.
                self.stdout.write(
                    "Stop requested during startup; not hosting a worker."
                )
                return
            await asyncio.gather(*runs)

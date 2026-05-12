"""
Management command to launch deploy-time pre-fetch actors.

Launches:
- ``ExtraLinkPrefetchActor`` — pre-fetches external documents and PubMed articles.
- ``PayerPolicyPrefetchActor`` — refreshes static payer medical-policy indexes.

Both actors run once per deployment and the command is non-blocking: if either
task hasn't completed within the timeout, the command exits cleanly and the
actor continues in the background.
"""

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Launch the extralink and payer-policy pre-fetch actors "
        "(both run once at deploy time, non-blocking)"
    )

    def handle(self, *args: str, **options: Any) -> None:
        import ray

        from fighthealthinsurance.extralink_prefetch_actor_ref import (
            extralink_prefetch_actor_ref,
        )
        from fighthealthinsurance.payer_policy_prefetch_actor_ref import (
            payer_policy_prefetch_actor_ref,
        )

        self.stdout.write("Starting deploy-time pre-fetch actors...")

        # Check Ray connectivity
        if not ray.is_initialized():
            try:
                ray.init(address="auto", ignore_reinit_error=True)
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"Failed to connect to Ray cluster: {e}")
                )
                self.stderr.write("Continuing despite Ray connection failure")
                return

        # Kick off each actor independently so a failure on one doesn't block
        # the other. ``tasks`` maps the Ray ObjectRef to a human-readable label
        # for clearer log output as each task settles.
        tasks: dict[Any, str] = {}

        try:
            extralink_actor, extralink_task = extralink_prefetch_actor_ref.start()
            self.stdout.write(f"Extralink actor launched: {extralink_actor}")
            self.stdout.write(f"Extralink pre-fetch task started: {extralink_task}")
            tasks[extralink_task] = "extralink"
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"Error launching extralink pre-fetch actor: {e}")
            )
            self.stderr.write("Continuing despite extralink pre-fetch failure")

        try:
            payer_actor, payer_task = payer_policy_prefetch_actor_ref.start()
            self.stdout.write(f"Payer-policy actor launched: {payer_actor}")
            self.stdout.write(f"Payer-policy pre-fetch task started: {payer_task}")
            tasks[payer_task] = "payer-policy"
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"Error launching payer-policy pre-fetch actor: {e}")
            )
            self.stderr.write("Continuing despite payer-policy pre-fetch failure")

        if not tasks:
            self.stderr.write(
                self.style.ERROR("No pre-fetch tasks were launched; nothing to wait on")
            )
            return

        # Wait for each task to settle, but don't block the deploy job on it.
        # ``ray.wait`` returns whatever has completed within the timeout; the
        # remainder keeps running in the background.
        timeout = 600  # 10 minutes total across both tasks
        self.stdout.write(
            f"Waiting up to {timeout}s for {len(tasks)} pre-fetch task(s)..."
        )

        pending = list(tasks.keys())
        ready, pending = ray.wait(pending, num_returns=len(pending), timeout=timeout)

        for task_ref in ready:
            label = tasks[task_ref]
            try:
                result = ray.get(task_ref)
                self.stdout.write(
                    self.style.SUCCESS(f"{label} pre-fetch completed: {result}")
                )
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"{label} pre-fetch raised: {e}"))

        for task_ref in pending:
            label = tasks[task_ref]
            self.stdout.write(
                f"{label} pre-fetch did not finish within {timeout}s; "
                "continuing in background (non-blocking)"
            )

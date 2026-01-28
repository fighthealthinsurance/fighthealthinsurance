"""
Management command to launch the extralink pre-fetch actor.

This command starts the actor that pre-fetches external documents and PubMed
articles at deploy time. It's designed to run once per deployment.
"""

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Launch the extralink pre-fetch actor (runs once at deploy time)"

    def handle(self, *args: str, **options: Any) -> None:
        import ray

        from fighthealthinsurance.extralink_prefetch_actor_ref import (
            extralink_prefetch_actor_ref,
        )

        self.stdout.write("Starting extralink pre-fetch actor...")

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

        try:
            actor_ref, task = extralink_prefetch_actor_ref.start()
            self.stdout.write(f"Actor launched: {actor_ref}")
            self.stdout.write(f"Pre-fetch task started: {task}")

            # Wait for task to complete (with timeout)
            # This is non-blocking for deployment - if it times out, we continue
            timeout = 600  # 10 minutes
            self.stdout.write(f"Waiting up to {timeout}s for pre-fetch to complete...")

            ready, _ = ray.wait([task], timeout=timeout)

            if ready:
                result = ray.get(ready[0])
                self.stdout.write(
                    self.style.SUCCESS("Pre-fetch completed successfully!")
                )
                self.stdout.write(f"Results: {result}")
            else:
                self.stdout.write(
                    f"Pre-fetch timed out after {timeout}s (continuing in background)"
                )
                self.stdout.write(
                    "This is normal - pre-fetch will continue asynchronously"
                )

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error launching pre-fetch actor: {e}"))
            self.stderr.write("Continuing despite pre-fetch failure (non-blocking)")
            # Don't raise - we don't want to fail deployment if pre-fetch fails

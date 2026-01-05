"""
Management command to launch the extralink pre-fetch actor.

This command starts the actor that pre-fetches external documents and PubMed
articles at deploy time. It's designed to run once per deployment.
"""

from typing import Any
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Launch the extralink pre-fetch actor (runs once at deploy time)"

    def handle(self, *args: str, **options: Any):
        import ray
        import time
        from fighthealthinsurance.extralink_prefetch_actor_ref import (
            extralink_prefetch_actor_ref,
        )

        print("Starting extralink pre-fetch actor...")

        try:
            actor_ref, task = extralink_prefetch_actor_ref.get
            print(f"Actor launched: {actor_ref}")
            print(f"Pre-fetch task started: {task}")

            # Wait for task to complete (with timeout)
            # This is non-blocking for deployment - if it times out, we continue
            timeout = 600  # 10 minutes
            print(f"Waiting up to {timeout}s for pre-fetch to complete...")

            ready, _ = ray.wait([task], timeout=timeout)

            if ready:
                result = ray.get(ready[0])
                print(f"Pre-fetch completed successfully!")
                print(f"Results: {result}")
            else:
                print(
                    f"Pre-fetch timed out after {timeout}s (continuing in background)"
                )
                print("This is normal - pre-fetch will continue asynchronously")

        except Exception as e:
            print(f"Error launching pre-fetch actor: {e}")
            print("Continuing despite pre-fetch failure (non-blocking)")
            # Don't raise - we don't want to fail deployment if pre-fetch fails

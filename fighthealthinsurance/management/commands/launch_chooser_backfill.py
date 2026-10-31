"""Start the chooser's launch-time backfill (chooser_tasks.launch_backfill).

By default as the one-shot ``ChooserBackfillActor`` on the Ray cluster,
non-blocking: the deploy's actor-launch job runs this and moves on while the
actor writes its cases, which takes hours. ``--inline`` runs the backfill in
this process instead, for a dev machine without Ray, or to watch one.
"""

import time
from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Write a batch of new chooser appeal cases once every current one "
        "has been judged (as a Ray actor, non-blocking; --inline to run here)"
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--inline",
            action="store_true",
            help="Run the backfill in this process rather than as a Ray actor",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=None,
            help="Tasks to write (default: CHOOSER_LAUNCH_BACKFILL_SIZE)",
        )
        parser.add_argument(
            "--min-votes",
            type=int,
            default=None,
            help=(
                "Votes every current appeal case needs before the backfill is "
                "due (default: CHOOSER_LAUNCH_BACKFILL_MIN_VOTES)"
            ),
        )
        parser.add_argument(
            "--wait",
            type=int,
            default=60,
            help="Seconds to wait for the actor before leaving it to run in the background",
        )
        parser.add_argument(
            "--ray-wait",
            type=int,
            default=120,
            help=(
                "Seconds to keep trying to reach the Ray cluster: the deploy "
                "job runs this first, possibly before the cluster answers"
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        count = options["count"]
        min_votes = options["min_votes"]
        # 0 or less would make every pool "fully judged" and write the whole
        # backfill before anyone voted; the setting has the same floor.
        if min_votes is not None and min_votes < 1:
            raise CommandError("--min-votes must be at least 1")
        if options["inline"]:
            from fighthealthinsurance.chooser_tasks import launch_backfill

            result = async_to_sync(launch_backfill)(count=count, min_votes=min_votes)
            self.stdout.write(f"Chooser backfill: {result}")
            return

        import ray

        from fighthealthinsurance.chooser_backfill_actor_ref import (
            chooser_backfill_actor_ref,
        )

        # The deploy job runs this before the polling actors (see
        # scripts/start-server.sh), when the cluster may not answer yet; the
        # polling-actor launch waits a minute for it on the same grounds.
        deadline = time.monotonic() + max(0, options["ray_wait"])
        while not ray.is_initialized():
            try:
                ray.init(address="auto", ignore_reinit_error=True)
            except Exception as e:
                if time.monotonic() >= deadline:
                    self.stderr.write(
                        self.style.ERROR(f"Failed to connect to Ray cluster: {e}")
                    )
                    self.stderr.write("Not starting the chooser backfill")
                    return
                self.stdout.write(f"Ray cluster not reachable yet ({e}); retrying")
                time.sleep(10)

        actor, task = chooser_backfill_actor_ref.start(count, min_votes)
        self.stdout.write(f"Chooser backfill actor launched: {actor}")

        # A backfill is hours of model calls; the launch job does not wait
        # for it. A short wait catches a backfill that is not due (it says
        # so at once) and a launch that failed outright.
        ready, _pending = ray.wait([task], timeout=options["wait"])
        if not ready:
            self.stdout.write(
                f"Chooser backfill still running after {options['wait']}s; "
                "continuing in the background (non-blocking)"
            )
            return
        try:
            result = ray.get(task)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Chooser backfill raised: {e}"))
            return
        # The actor reports a failed backfill as {"error": ...} rather than
        # raising, so the launch job's log must not call it a success.
        if isinstance(result, dict) and "error" in result:
            self.stderr.write(
                self.style.ERROR(f"Chooser backfill failed: {result['error']}")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Chooser backfill: {result}"))

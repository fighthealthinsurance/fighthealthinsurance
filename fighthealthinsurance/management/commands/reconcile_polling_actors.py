"""Put back any polling actor that has gone missing.

The polling actors are created once per deploy, by the ``web-actor-launch``
Job, and they are ``lifetime="detached"`` so they outlive the pod that made
them. What nothing covered is them DYING: the Ray head going away takes every
actor with it, and since the launcher only runs at deploy time there is
nothing to notice or to put them back. Production sat with 0 of 5 alive after
a node reboot until someone happened to read the status page -- email polling,
chooser refill and three refresh loops all silently stopped, and a deploy was
the only thing that would have fixed it.

This is the missing half: cheap, idempotent, and safe to run on a schedule.
It relaunches only what is actually absent, so a healthy cluster costs one
health check and nothing else.

Exit codes: 0 when every actor is alive at the end (including when nothing
needed doing), 1 when any is still missing, so a CronJob failure means
"could not restore" rather than "checked".
"""

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Relaunch any polling actor that is missing (idempotent)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what is missing without relaunching anything.",
        )

    def handle(self, *args: str, **options: Any) -> None:
        import sys

        from fighthealthinsurance.actor_health_status import (
            check_actor_health,
            relaunch_actors,
        )
        from fighthealthinsurance.base_actor_ref import ray_cluster_available

        dry_run: bool = options.get("dry_run", False)

        # Gate before touching Ray at all. Ray auto-initializes on first use
        # and that is NOT a no-op: with no cluster configured it starts a
        # brand-new local one inside this process, which would then report
        # every actor missing, "restore" them into a cluster that dies with
        # the pod, and report success. Answering "no cluster" is the honest
        # outcome, and it is a failure because a scheduled reconcile that
        # cannot see the cluster has not done its job.
        if not ray_cluster_available():
            self.stdout.write(
                self.style.ERROR(
                    "No Ray cluster reachable; not reconciling. "
                    "(Set RAY_ADDRESS to the cluster, or run this where it is reachable.)"
                )
            )
            sys.exit(1)

        before = check_actor_health()
        missing = [d for d in before.get("details", []) if not d.get("alive")]
        total = before.get("total_actors", 0)

        if not missing:
            self.stdout.write(
                self.style.SUCCESS(f"All {total} polling actors alive; nothing to do.")
            )
            return

        names = ", ".join(sorted(d.get("name", "?") for d in missing))
        self.stdout.write(f"Missing {len(missing)} of {total}: {names}")

        if dry_run:
            self.stdout.write("--dry-run: not relaunching.")
            sys.exit(1)

        # force=False is the whole point: it attaches to actors that are alive
        # and only creates the ones that are not, so a partial outage is
        # repaired without disturbing the survivors. A force relaunch here
        # would kill healthy actors mid-work on every scheduled run.
        results = relaunch_actors(force=False)
        for actor_name in sorted(results):
            result = results[actor_name]
            status = result.get("status")
            if status in ("launched", "exists", "alive"):
                self.stdout.write(self.style.SUCCESS(f"  {actor_name}: {status}"))
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  {actor_name}: {status} - {result.get('error', 'unknown error')}"
                    )
                )

        # Report on what is true afterwards, not on what relaunch_actors
        # claimed: an actor can be created and still fail to come up.
        after = check_actor_health()
        still_missing = [d for d in after.get("details", []) if not d.get("alive")]
        alive = after.get("alive_actors", 0)
        total_after = after.get("total_actors", 0)

        if still_missing:
            names = ", ".join(sorted(d.get("name", "?") for d in still_missing))
            self.stdout.write(
                self.style.ERROR(
                    f"Still missing after relaunch ({alive}/{total_after} alive): {names}"
                )
            )
            sys.exit(1)

        self.stdout.write(
            self.style.SUCCESS(f"Restored; {alive}/{total_after} polling actors alive.")
        )

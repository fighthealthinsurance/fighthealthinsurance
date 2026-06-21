from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Launch the polling actors"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force relaunch actors by killing existing ones first",
        )

    def handle(self, *args: str, **options: Any):
        force = options.get("force", False)

        if force:
            self.stdout.write("Force relaunching polling actors...")
            from fighthealthinsurance.actor_health_status import relaunch_actors

            results = relaunch_actors(force=True)

            for actor_name, result in results.items():
                if result.get("status") == "launched":
                    self.stdout.write(
                        self.style.SUCCESS(f"✓ {actor_name}: {result.get('status')}")
                    )
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"✗ {actor_name}: {result.get('status')} - {result.get('error', 'unknown error')}"
                        )
                    )
        else:
            from fighthealthinsurance.polling_actor_setup import cpar, epar, fpar, ipar

            self.stdout.write(f"Loaded actors: {epar} {fpar} {cpar} {ipar}")
            self.stdout.write(self.style.SUCCESS("Polling actors loaded successfully"))

        # Run the deployment-time model probe here so it happens once, in a
        # single container, rather than on every web worker. Emails a report
        # of any unreachable backends to the support alias for investigation.
        self._probe_models()

    def _probe_models(self) -> None:
        from fighthealthinsurance.ml.startup_probe import run_startup_model_probe

        results = run_startup_model_probe()
        if results is None:
            return
        dead = [(name, err) for name, ok, err in results if not ok]
        if dead:
            self.stdout.write(
                self.style.WARNING(
                    f"Model probe: {len(dead)}/{len(results)} backend(s) "
                    "unreachable; report emailed to support."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Model probe: all {len(results)} backend(s) responded."
                )
            )

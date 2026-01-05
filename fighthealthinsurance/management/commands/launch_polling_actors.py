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
            from fighthealthinsurance.polling_actor_setup import epar, fpar, cpar

            self.stdout.write(f"Loaded actors: {epar} {fpar} {cpar}")
            self.stdout.write(self.style.SUCCESS("Polling actors loaded successfully"))

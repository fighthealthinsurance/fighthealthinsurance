from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Probe every configured model backend with a tiny 'Hello' inference and "
        "email a report of any unreachable backends to the support alias. Intended "
        "to run once per deployment from a single container (e.g. right after "
        "launch_polling_actors). No-ops under the test configs and when "
        "FHI_STARTUP_MODEL_PROBE=0."
    )

    def handle(self, *args: str, **options: Any):
        from fighthealthinsurance.ml.startup_probe import run_startup_model_probe

        results = run_startup_model_probe()
        if results is None:
            self.stdout.write("Model probe skipped (disabled or test environment).")
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

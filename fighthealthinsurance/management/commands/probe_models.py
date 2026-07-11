from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "DEPRECATED: superseded by `check_model_backends`, which adds "
        "categorized failures, leader election, persistence, and a "
        "consolidated alert email (start-server.sh now runs that on deploy). "
        "This older probe hits every configured backend with a tiny 'Hello' "
        "inference and emails a report of any unreachable backends. No-ops "
        "under the test configs and when FHI_STARTUP_MODEL_PROBE=0."
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

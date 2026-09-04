"""The intake outbox relay: re-deliver pending intake-journey events.

An ``IntakeJourneyEvent`` without ``acked_at`` is a delivery that a crash or
a Temporal blip interrupted after the commit. This runs every minute as its
own CronJob (k8s/temporal/intake-outbox-cronjob.yaml) -- independent of the
web and Ray processes on purpose -- and is also the manual entry point::

    python manage.py deliver_intake_events [--limit 200]

Each due row is claimed (SELECT ... FOR UPDATE SKIP LOCKED where supported)
and delivered inside its own exception boundary; failures back off via
``next_attempt_at`` and are never dropped. Logs a backlog count each run.
Inert while the intake journey flags are off.
"""

from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance import intake_outbox


class Command(BaseCommand):
    help = "Re-deliver intake-journey events whose Temporal ack never landed."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--limit", type=int, default=200, help="Max rows per run (default 200)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        counts = intake_outbox.sweep(options["limit"])
        self.stdout.write(str(counts))

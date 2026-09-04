"""The intake outbox relay: re-deliver pending intake-journey events.

An ``IntakeJourneyEvent`` without ``acked_at`` is a delivery that a crash or
a Temporal blip interrupted after the commit. This runs every minute as its
own CronJob (k8s/temporal/intake-outbox-cronjob.yaml) -- independent of the
web and Ray processes on purpose -- and is also the manual entry point::

    python manage.py deliver_intake_events [--limit 200] [--time-budget 240]

Two phases per run: a short locked claim that commits, then delivery with
no database lock held, one Temporal client for the batch, a per-call
timeout, and conditional acks; failures back off via ``next_attempt_at``
and are never dropped.

Every run logs backlog, oldest pending age, and attempted / delivered /
failed counts. Exit status is the operational signal: rows waiting on
backoff are normal (exit 0); a SYSTEMIC failure -- the Temporal client
could not connect, or every attempted delivery failed at the transport
level -- exits non-zero so the CronJob shows red. Inert while the intake
journey flags are off.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance import intake_outbox


class Command(BaseCommand):
    help = "Re-deliver intake-journey events whose Temporal ack never landed."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--limit", type=int, default=200, help="Max rows per run (default 200)."
        )
        parser.add_argument(
            "--time-budget",
            type=float,
            default=intake_outbox.DEFAULT_TIME_BUDGET_SECONDS,
            help="Stop starting new rows after this many seconds (default 240).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        limit = options["limit"]
        if limit is not None and limit < 0:
            raise CommandError(f"--limit must be >= 0 (got {limit})")
        counts = intake_outbox.sweep(limit, options["time_budget"])
        self.stdout.write(
            "intake outbox relay: "
            f"backlog={counts.get('backlog', 0)} "
            f"oldest_pending_seconds={counts.get('oldest_pending_seconds', 0.0):.0f} "
            f"attempted={counts.get('attempted', 0)} "
            f"delivered={counts.get('delivered', 0)} "
            f"failed={counts.get('failed', 0)} "
            f"lost_claim={counts.get('lost_claim', 0)} "
            f"deferred={counts.get('deferred', 0)}"
        )
        if counts.get("deferred"):
            self.stdout.write(
                f"time budget reached: {counts['deferred']} claimed row(s) left "
                "for the next run"
            )
        if counts.get("systemic"):
            raise CommandError(
                "intake outbox relay: systemic failure -- "
                + (
                    f"Temporal client could not connect ({counts['client_error']})"
                    if counts.get("client_error")
                    else "every attempted delivery failed at the transport level"
                )
            )

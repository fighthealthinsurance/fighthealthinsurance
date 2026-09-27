"""Compute a chat routing policy from recent chat turns and store it.

Reads ChatTurn metadata over a window, runs ``ml/chat_policy.compute_policy``
and appends one ChatRoutingPolicy row. Rows are never edited; once the new
row is stored, rows older than 30 days are deleted. Run it by hand, or from
a CronJob. Chat follows the newest row only while
FHI_CHAT_POLICY_APPLY is on; either way the row shows on the staff ML Model
Usage Dashboard.

Prints names and numbers only.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Compute a chat routing policy from recent chat turns and store it."

    def add_arguments(self, parser: Any) -> None:
        from fighthealthinsurance.ml.chat_policy import DEFAULT_WINDOW_MINUTES

        parser.add_argument(
            "--window-minutes",
            type=int,
            default=DEFAULT_WINDOW_MINUTES,
            help=(
                "How many minutes of chat turns to compute from "
                f"(default {DEFAULT_WINDOW_MINUTES})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the policy without storing it.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from fighthealthinsurance.ml import chat_policy
        from fighthealthinsurance.models import ChatRoutingPolicy

        window = options["window_minutes"]
        if not 1 <= window <= 30 * 24 * 60:
            raise CommandError("--window-minutes must be between 1 and 43200")

        if options["dry_run"]:
            policy = chat_policy.compute_policy(
                chat_policy.aggregate_chat_turns(window),
                chat_policy.configured_daily_call_caps(),
            )
            fields = policy.row_fields()
            self.stdout.write("Dry run: nothing stored.")
        else:
            row = chat_policy.compute_and_store_chat_policy(
                window_minutes=window, source=ChatRoutingPolicy.Source.MANUAL
            )
            fields = {
                name: getattr(row, name)
                for name in chat_policy.DEFAULT_POLICY.row_fields()
            }
            self.stdout.write(f"Stored chat routing policy {row.pk}.")
        for name, value in fields.items():
            self.stdout.write(f"  {name}: {value}")

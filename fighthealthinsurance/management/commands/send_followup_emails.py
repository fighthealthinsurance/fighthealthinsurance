"""Management command to send follow-up emails to users who opted in."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from fighthealthinsurance.followup_emails import FollowUpEmailSender


class Command(BaseCommand):
    help = "Send follow-up emails to users who opted in for follow-up"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--count",
            type=int,
            help="Maximum number of emails to send (default: all pending)",
            default=None,
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print candidates without sending emails",
        )

    def handle(self, *args: str, **options: Any) -> None:
        sender = FollowUpEmailSender()
        count = options.get("count")
        dry_run = options.get("dry_run", False)

        candidates = sender.find_candidates()
        candidate_count = candidates.count()

        if candidate_count == 0:
            self.stdout.write(
                self.style.SUCCESS("No pending follow-up emails to send.")
            )
            return

        self.stdout.write(f"Found {candidate_count} pending follow-up emails.")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode - not sending emails."))
            for candidate in candidates[:count] if count else candidates:
                self.stdout.write(f"  Would send to: {candidate.email}")
            return

        sent_count = sender.send_all(count=count)
        self.stdout.write(
            self.style.SUCCESS(f"Successfully sent {sent_count} follow-up emails.")
        )

"""Management command to send calendar reminder emails at scheduled intervals."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from fighthealthinsurance.calendar_emails import CalendarReminderSender


class Command(BaseCommand):
    help = (
        "Send calendar reminder emails at 2/30/90 day intervals for insurance appeals"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--count",
            type=int,
            help="Maximum number of reminder emails to send (default: all pending)",
            default=None,
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print candidates without sending emails",
        )

    def handle(self, *args: str, **options: Any) -> None:
        sender = CalendarReminderSender()
        count = options.get("count")
        dry_run = options.get("dry_run", False)

        candidates = sender._find_candidates()
        candidate_count = candidates.count()

        if candidate_count == 0:
            self.stdout.write(
                self.style.SUCCESS("No pending calendar reminders to send.")
            )
            return

        self.stdout.write(f"Found {candidate_count} pending calendar reminders.")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode - not sending emails."))
            for candidate in candidates[:count] if count else candidates:
                self.stdout.write(
                    f"  Would send {candidate.get_reminder_type_display()} to: {candidate.email}"
                )
            return

        sent_count = sender.send_all(count=count)
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully sent {sent_count} calendar reminder emails."
            )
        )

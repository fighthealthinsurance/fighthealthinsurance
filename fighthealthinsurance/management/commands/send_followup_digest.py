"""Management command to send weekly follow-up digest emails to patient users."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from fighthealthinsurance.followup_digest import FollowupDigestSender


class Command(BaseCommand):
    help = "Send weekly follow-up digest emails to patient users with upcoming tasks"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--count",
            type=int,
            help="Maximum number of digest emails to send (default: all candidates)",
            default=None,
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print candidates without sending emails",
        )

    def handle(self, *args: str, **options: Any) -> None:
        sender = FollowupDigestSender()
        count = options.get("count")
        dry_run = options.get("dry_run", False)

        candidates = sender._find_candidates()
        candidate_count = candidates.count()

        if candidate_count == 0:
            self.stdout.write(
                self.style.SUCCESS("No patient users need follow-up digests.")
            )
            return

        self.stdout.write(
            f"Found {candidate_count} patient users for follow-up digests."
        )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("Dry run mode - not sending emails.")
            )
            for candidate in candidates[:count] if count else candidates:
                user = candidate.user
                context = sender._generate_digest_context(candidate)
                self.stdout.write(
                    f"  Would send digest to: {user.email} "
                    f"(Upcoming: {context['upcoming_count']}, "
                    f"Overdue: {context['overdue_count']}, "
                    f"Active: {context['active_count']})"
                )
            return

        sent_count = sender.send_all(count=count)
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully sent {sent_count} follow-up digest emails."
            )
        )

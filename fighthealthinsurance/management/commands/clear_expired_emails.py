"""Management command to clear email addresses from denials for users who didn't opt in for storage."""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Q
from django.utils import timezone

from fighthealthinsurance.models import Denial, FollowUpSched


class Command(BaseCommand):
    help = "Clear email addresses from denials 30 days after follow-up was sent for users who didn't opt in to store their data longer"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            help="Number of days after follow-up was sent to clear emails (default: 30)",
            default=30,
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print candidates without clearing emails",
        )

    def handle(self, *args: str, **options: Any) -> None:
        days = options.get("days", 30)
        dry_run = options.get("dry_run", False)

        cutoff_datetime = timezone.now() - datetime.timedelta(days=days)
        # Safety check: don't clear emails for denials created in the last 90 days
        safety_cutoff_date = datetime.date.today() - datetime.timedelta(days=90)

        # Find denials that:
        # 1. Have a raw_email set (user opted in for follow-up)
        # 2. Have had follow-up emails sent
        # 3. The follow-up was sent more than `days` ago
        # 4. User did not request more follow-up (meaning they didn't opt in to longer storage)

        # First, find follow-up schedules that were sent more than `days` ago
        # and where the user didn't request more follow-up
        sent_followups = FollowUpSched.objects.filter(
            follow_up_sent=True,
            follow_up_sent_date__lt=cutoff_datetime,
        ).select_related("denial_id")

        # Get the denial IDs from those follow-ups
        denial_ids_with_old_followups = set(
            sent_followups.values_list("denial_id__denial_id", flat=True)
        )

        # Now find denials that:
        # 1. Have a raw_email (user opted in for initial follow-up)
        # 2. Don't have any pending or recent follow-ups (user didn't opt in for more)
        # 3. Had their follow-up sent more than `days` ago

        # Get denials that have recent or pending follow-ups (these should NOT be cleared)
        denials_with_recent_or_pending_followups = set(
            FollowUpSched.objects.filter(
                Q(follow_up_sent=False)  # Pending follow-up
                | Q(follow_up_sent_date__gte=cutoff_datetime)  # Recent follow-up
            ).values_list("denial_id__denial_id", flat=True)
        )

        # Filter to only denials that have raw_email set, had old follow-ups sent,
        # and don't have recent or pending follow-ups
        # Also exclude denials created in the last 90 days for safety
        candidates = (
            Denial.objects.filter(
                denial_id__in=denial_ids_with_old_followups,
                date__lt=safety_cutoff_date,  # Safety check: only clear emails for older denials
            )
            .exclude(
                denial_id__in=denials_with_recent_or_pending_followups,
            )
            .exclude(
                raw_email__isnull=True,
            )
            .exclude(
                raw_email="",
            )
        )

        candidate_count = candidates.count()

        if candidate_count == 0:
            self.stdout.write(self.style.SUCCESS("No emails to clear."))
            return

        self.stdout.write(f"Found {candidate_count} denials with emails to clear.")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run mode - not clearing emails."))
            for denial in candidates[:10]:  # Only show first 10 in dry run
                self.stdout.write(
                    f"  Would clear email for denial {denial.denial_id} "
                    f"(hashed: {denial.hashed_email[:20]}...)"
                )
            if candidate_count > 10:
                self.stdout.write(f"  ... and {candidate_count - 10} more")
            return

        # Clear the raw_email field for these denials
        # First, capture the denial IDs before the update
        denial_ids_to_clear = list(candidates.values_list("denial_id", flat=True))

        cleared_count = candidates.update(raw_email=None)

        # Also clear emails from the FollowUpSched entries for these denials
        FollowUpSched.objects.filter(denial_id__in=denial_ids_to_clear).update(email="")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully cleared emails from {cleared_count} denials."
            )
        )

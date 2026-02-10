"""
Management command to purge old fuzz attempt records.

Usage:
    python manage.py purge_fuzz_attempts [--days N] [--dry-run]

Deletes FuzzAttempt records older than the retention period (default from
FUZZ_GUARD_RETENTION_DAYS setting, or 3 days if not set).
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone


class Command(BaseCommand):
    help = "Purge fuzz attempt records older than retention period"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            help="Number of days to retain (default: FUZZ_GUARD_RETENTION_DAYS setting or 3)",
            default=None,
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print count without deleting",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Number of records to delete per batch (default: 1000)",
        )

    def handle(self, *args: str, **options: Any) -> None:
        from django.conf import settings

        from fighthealthinsurance.models import FuzzAttempt

        # Determine retention days
        days_opt = options.get("days")
        if days_opt is not None:
            days = int(days_opt)
        else:
            days = int(getattr(settings, "FUZZ_GUARD_RETENTION_DAYS", 3))

        dry_run = options.get("dry_run", False)
        batch_size = options.get("batch_size", 1000)

        # Calculate cutoff date
        cutoff = timezone.now() - datetime.timedelta(days=days)

        # Find old records
        old_records = FuzzAttempt.objects.filter(created_at__lt=cutoff)
        count = old_records.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(f"No fuzz attempt records older than {days} days.")
            )
            return

        self.stdout.write(
            f"Found {count} records older than {days} days (cutoff: {cutoff})"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run - no records deleted."))
            return

        # Delete in batches to avoid memory issues
        deleted_total = 0
        files_deleted = 0

        while True:
            # Get batch of IDs
            batch_ids = list(
                FuzzAttempt.objects.filter(created_at__lt=cutoff).values_list(
                    "id", flat=True
                )[:batch_size]
            )

            if not batch_ids:
                break

            # Delete associated files first
            batch_records = FuzzAttempt.objects.filter(id__in=batch_ids)
            for record in batch_records:
                if record.encrypted_blob:
                    try:
                        record.encrypted_blob.delete(save=False)
                        files_deleted += 1
                    except Exception as e:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Failed to delete blob for record {record.id}: {e}"
                            )
                        )

            # Delete records
            deleted, _ = FuzzAttempt.objects.filter(id__in=batch_ids).delete()
            deleted_total += deleted
            self.stdout.write(f"  Deleted batch of {deleted} records...")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully purged {deleted_total} fuzz attempt records "
                f"and {files_deleted} encrypted blob files."
            )
        )

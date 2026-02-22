"""
Management command to purge old fuzz attempt records.

Usage:
    python manage.py purge_fuzz_attempts [--days N] [--dry-run]

Deletes FuzzAttempt records older than the retention period (default from
FUZZ_GUARD_RETENTION_DAYS setting, or 3 days if not set).
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
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
        if days_opt is None:
            days_opt = getattr(settings, "FUZZ_GUARD_RETENTION_DAYS", 3)
        try:
            days = int(days_opt)
        except (TypeError, ValueError) as exc:
            raise CommandError("--days must be a positive integer.") from exc
        if days <= 0:
            raise CommandError("--days must be greater than 0.")

        dry_run = options.get("dry_run", False)
        batch_size_opt = options.get("batch_size", 1000)
        try:
            batch_size = int(batch_size_opt)
        except (TypeError, ValueError) as exc:
            raise CommandError("--batch-size must be a positive integer.") from exc
        if batch_size <= 0:
            raise CommandError("--batch-size must be greater than 0.")

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

            # Delete associated files first, skip records where file deletion fails
            batch_records = FuzzAttempt.objects.filter(id__in=batch_ids)
            skip_ids = set()
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
                        skip_ids.add(record.id)

            # Delete records (excluding those with failed file deletion)
            delete_ids = [id for id in batch_ids if id not in skip_ids]
            if not delete_ids:
                break
            deleted, _ = FuzzAttempt.objects.filter(id__in=delete_ids).delete()
            deleted_total += deleted
            self.stdout.write(f"  Deleted batch of {deleted} records...")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully purged {deleted_total} fuzz attempt records "
                f"and {files_deleted} encrypted blob files."
            )
        )

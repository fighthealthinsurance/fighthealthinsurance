"""
Tests for purge_fuzz_attempts management command.
"""

import datetime
from io import StringIO
from unittest.mock import patch, MagicMock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance.models import FuzzAttempt


class TestPurgeFuzzAttemptsBasic(TestCase):
    """Test basic purge functionality."""

    def test_purges_old_records(self):
        """Should delete records older than retention days."""
        # Create old record
        old_attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        # Manually set created_at to 10 days ago
        old_created = timezone.now() - datetime.timedelta(days=10)
        FuzzAttempt.objects.filter(id=old_attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        self.assertEqual(FuzzAttempt.objects.count(), 0)
        self.assertIn("Successfully purged", out.getvalue())

    def test_keeps_recent_records(self):
        """Should keep records newer than retention days."""
        # Create recent record
        FuzzAttempt.objects.create(
            ip_hash="b" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        self.assertEqual(FuzzAttempt.objects.count(), 1)
        self.assertIn("No fuzz attempt records older than", out.getvalue())

    def test_mixed_old_and_new(self):
        """Should only delete old records, keeping new ones."""
        # Create old and new records in one DB write to reduce sqlite flakiness.
        records = FuzzAttempt.objects.bulk_create(
            [
                FuzzAttempt(
                    ip_hash="a" * 64,
                    ip_prefix="192.168.1.0/24",
                    method="GET",
                    path="/old",
                    status_returned=400,
                    reason='["test"]',
                    score=50,
                    encrypted_blob=None,
                ),
                FuzzAttempt(
                    ip_hash="b" * 64,
                    ip_prefix="192.168.2.0/24",
                    method="GET",
                    path="/new",
                    status_returned=400,
                    reason='["test"]',
                    score=50,
                    encrypted_blob=None,
                ),
            ]
        )

        old_created = timezone.now() - datetime.timedelta(days=10)
        FuzzAttempt.objects.filter(id=records[0].id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        self.assertEqual(FuzzAttempt.objects.count(), 1)
        remaining = FuzzAttempt.objects.first()
        self.assertEqual(remaining.path, "/new")


class TestPurgeFuzzAttemptsSettings(TestCase):
    """Test purge command settings and arguments."""

    @override_settings(FUZZ_GUARD_RETENTION_DAYS=7)
    def test_uses_setting_for_default_days(self):
        """Should use FUZZ_GUARD_RETENTION_DAYS by default."""
        # Create record 5 days old (within 7 day retention)
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        old_created = timezone.now() - datetime.timedelta(days=5)
        FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", stdout=out)

        # Should be kept because 5 < 7
        self.assertEqual(FuzzAttempt.objects.count(), 1)

    @override_settings(FUZZ_GUARD_RETENTION_DAYS=7)
    def test_custom_days_argument_overrides_setting(self):
        """--days argument should override FUZZ_GUARD_RETENTION_DAYS."""
        # Create record 5 days old
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        old_created = timezone.now() - datetime.timedelta(days=5)
        FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=3", stdout=out)

        # Should be deleted because 5 > 3
        self.assertEqual(FuzzAttempt.objects.count(), 0)


class TestPurgeFuzzAttemptsDryRun(TestCase):
    """Test dry-run mode."""

    def test_dry_run_does_not_delete(self):
        """--dry-run should not delete any records."""
        # Create old record
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        old_created = timezone.now() - datetime.timedelta(days=10)
        FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", "--dry-run", stdout=out)

        # Record should still exist
        self.assertEqual(FuzzAttempt.objects.count(), 1)

    def test_dry_run_shows_count(self):
        """--dry-run should show how many would be deleted."""
        # Create 3 old records with bulk operations to reduce sqlite churn.
        old_created = timezone.now() - datetime.timedelta(days=10)
        with patch("django.utils.timezone.now", return_value=old_created):
            FuzzAttempt.objects.bulk_create(
                [
                    FuzzAttempt(
                        ip_hash=chr(ord("a") + i) * 64,
                        ip_prefix=f"192.168.{i}.0/24",
                        method="GET",
                        path=f"/test/{i}",
                        status_returned=400,
                        reason='["test"]',
                        score=50,
                        encrypted_blob=None,
                    )
                    for i in range(3)
                ]
            )

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", "--dry-run", stdout=out)

        output = out.getvalue()
        self.assertIn("Found 3 records", output)
        self.assertIn("Dry run", output)

    def test_dry_run_warning_message(self):
        """--dry-run output should include warning about no deletion."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        old_created = timezone.now() - datetime.timedelta(days=10)
        FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", "--dry-run", stdout=out)

        self.assertIn("no records deleted", out.getvalue().lower())


class TestPurgeFuzzAttemptsBatchDeletion(TestCase):
    """Test batch deletion functionality."""

    def test_batch_deletion_works(self):
        """Should delete in batches correctly."""
        # Create 15 old records
        for i in range(15):
            attempt = FuzzAttempt.objects.create(
                ip_hash=f"{i:064d}",
                ip_prefix=f"192.168.{i % 256}.0/24",
                method="GET",
                path=f"/test/{i}",
                status_returned=400,
                reason='["test"]',
                score=50,
                encrypted_blob=None,
            )
            old_created = timezone.now() - datetime.timedelta(days=10)
            FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", "--batch-size=5", stdout=out)

        # All should be deleted
        self.assertEqual(FuzzAttempt.objects.count(), 0)
        # Should show batch deletions
        output = out.getvalue()
        self.assertIn("Deleted batch", output)

    def test_batch_size_argument(self):
        """--batch-size argument should control batch size."""
        # Create 10 old records with bulk operations to reduce sqlite churn.
        old_created = timezone.now() - datetime.timedelta(days=10)
        with patch("django.utils.timezone.now", return_value=old_created):
            FuzzAttempt.objects.bulk_create(
                [
                    FuzzAttempt(
                        ip_hash=f"{i:064d}",
                        ip_prefix=f"192.168.{i}.0/24",
                        method="GET",
                        path=f"/test/{i}",
                        status_returned=400,
                        reason='["test"]',
                        score=50,
                        encrypted_blob=None,
                    )
                    for i in range(10)
                ]
            )

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", "--batch-size=3", stdout=out)

        self.assertEqual(FuzzAttempt.objects.count(), 0)


class TestPurgeFuzzAttemptsNoRecords(TestCase):
    """Test behavior when no records to purge."""

    def test_no_records_message(self):
        """Should show appropriate message when no records exist."""
        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        self.assertIn("No fuzz attempt records", out.getvalue())

    def test_no_old_records_message(self):
        """Should show message when only recent records exist."""
        # Create recent record
        FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        self.assertIn("No fuzz attempt records older than", out.getvalue())


class TestPurgeFuzzAttemptsOutput(TestCase):
    """Test command output formatting."""

    def test_success_message_includes_count(self):
        """Success message should include deleted count."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        old_created = timezone.now() - datetime.timedelta(days=10)
        FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        self.assertIn("1", out.getvalue())
        self.assertIn("Successfully purged", out.getvalue())

    def test_output_shows_cutoff_date(self):
        """Output should show cutoff date."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            encrypted_blob=None,
        )
        old_created = timezone.now() - datetime.timedelta(days=10)
        FuzzAttempt.objects.filter(id=attempt.id).update(created_at=old_created)

        out = StringIO()
        call_command("purge_fuzz_attempts", "--days=5", stdout=out)

        output = out.getvalue()
        self.assertIn("cutoff", output.lower())

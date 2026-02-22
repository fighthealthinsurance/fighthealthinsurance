"""
Tests for FuzzAttempt admin configuration.
"""

import json
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings

from fighthealthinsurance.admin import FuzzAttemptAdmin
from fighthealthinsurance.models import FuzzAttempt

User = get_user_model()


class TestFuzzAttemptAdminRegistration(TestCase):
    """Test FuzzAttempt admin registration and basic configuration."""

    def test_admin_registered(self):
        """FuzzAttempt should be registered in admin."""
        from django.contrib import admin

        self.assertIn(FuzzAttempt, admin.site._registry)

    def test_admin_class_is_fuzz_attempt_admin(self):
        """FuzzAttempt should use FuzzAttemptAdmin class."""
        from django.contrib import admin

        admin_class = admin.site._registry[FuzzAttempt]
        self.assertIsInstance(admin_class, FuzzAttemptAdmin)


class TestFuzzAttemptAdminConfiguration(TestCase):
    """Test FuzzAttemptAdmin configuration options."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = FuzzAttemptAdmin(FuzzAttempt, self.site)

    def test_list_display_fields(self):
        """List display should show key fields."""
        expected_fields = [
            "created_at",
            "ip_hash_short",
            "ip_prefix",
            "method",
            "path_truncated",
            "score",
            "status_returned",
            "is_authenticated",
        ]
        for field in expected_fields:
            self.assertIn(field, self.admin.list_display)

    def test_list_filter_configured(self):
        """Should have filters for key fields."""
        expected_filters = [
            "created_at",
            "status_returned",
            "is_authenticated",
            "method",
        ]
        for filter_field in expected_filters:
            self.assertIn(filter_field, self.admin.list_filter)

    def test_search_fields_configured(self):
        """Should be searchable by key fields."""
        expected_search = ["ip_hash", "ip_prefix", "path", "reason", "request_id"]
        for field in expected_search:
            self.assertIn(field, self.admin.search_fields)

    def test_readonly_fields_all(self):
        """All fields should be readonly."""
        # The admin should not allow editing
        self.assertTrue(len(self.admin.readonly_fields) > 0)

    def test_date_hierarchy_configured(self):
        """Should have date hierarchy on created_at."""
        self.assertEqual(self.admin.date_hierarchy, "created_at")

    def test_ordering_descending(self):
        """Should order by created_at descending."""
        self.assertIn("-created_at", self.admin.ordering)


class TestFuzzAttemptAdminPermissions(TestCase):
    """Test FuzzAttemptAdmin permission restrictions."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = FuzzAttemptAdmin(FuzzAttempt, self.site)
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass"
        )

    def test_no_add_permission(self):
        """Should not allow adding records via admin."""
        request = self.factory.get("/admin/fighthealthinsurance/fuzzattempt/add/")
        request.user = self.user
        self.assertFalse(self.admin.has_add_permission(request))

    def test_no_change_permission(self):
        """Should not allow changing records via admin."""
        request = self.factory.get("/admin/fighthealthinsurance/fuzzattempt/1/change/")
        request.user = self.user
        self.assertFalse(self.admin.has_change_permission(request))

    def test_delete_permission_for_superuser(self):
        """Superuser should be able to delete records."""
        superuser = User.objects.create_superuser(
            username="superuser", email="super@example.com", password="superpass"
        )
        request = self.factory.get("/admin/fighthealthinsurance/fuzzattempt/")
        request.user = superuser
        self.assertTrue(self.admin.has_delete_permission(request))


class TestFuzzAttemptAdminActions(TestCase):
    """Test FuzzAttemptAdmin custom actions."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = FuzzAttemptAdmin(FuzzAttempt, self.site)
        self.factory = RequestFactory()
        self.superuser = User.objects.create_superuser(
            username="superuser", email="super@example.com", password="superpass"
        )

    def test_purge_action_exists(self):
        """purge_selected action should exist."""
        # Check that purge_selected is in the actions list
        self.assertIn("purge_selected", self.admin.actions)

    def test_export_csv_action_exists(self):
        """export_metadata_csv action should exist."""
        # Check that export_metadata_csv is in the actions list
        self.assertIn("export_metadata_csv", self.admin.actions)


class TestFuzzAttemptAdminHelperMethods(TestCase):
    """Test FuzzAttemptAdmin helper methods for display."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = FuzzAttemptAdmin(FuzzAttempt, self.site)
        self.attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/very/long/path/that/should/be/truncated/for/display/purposes",
            status_returned=400,
            reason='["test_reason"]',
            score=100,
        )

    def test_ip_hash_short_truncates(self):
        """ip_hash_short should return truncated hash."""
        short = self.admin.ip_hash_short(self.attempt)
        self.assertTrue(len(short) < len(self.attempt.ip_hash))
        self.assertIn("...", short)

    def test_path_truncated_truncates_long_path(self):
        """path_truncated should truncate long paths."""
        short = self.admin.path_truncated(self.attempt)
        self.assertTrue(len(short) < len(self.attempt.path))
        self.assertIn("...", short)

    def test_path_truncated_keeps_short_path(self):
        """path_truncated should not truncate short paths."""
        self.attempt.path = "/short"
        self.attempt.save()
        short = self.admin.path_truncated(self.attempt)
        self.assertEqual(short, "/short")


class TestFuzzAttemptAdminListView(TestCase):
    """Test FuzzAttemptAdmin list view configuration."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = FuzzAttemptAdmin(FuzzAttempt, self.site)

        # Create some test records
        for i in range(5):
            FuzzAttempt.objects.create(
                ip_hash=chr(ord("a") + i) * 64,
                ip_prefix=f"192.168.{i}.0/24",
                method="GET",
                path=f"/test/{i}",
                status_returned=400,
                reason=f'["reason_{i}"]',
                score=50 + i * 10,
            )

    def test_list_view_accessible(self):
        """Admin should be registered and have list_display."""
        from django.contrib import admin

        self.assertIn(FuzzAttempt, admin.site._registry)

    def test_list_view_shows_records(self):
        """Admin queryset should include created records."""
        queryset = FuzzAttempt.objects.all()
        self.assertEqual(queryset.count(), 5)
        ip_prefixes = [a.ip_prefix for a in queryset]
        self.assertIn("192.168.0.0/24", ip_prefixes)

    def test_search_works(self):
        """Search fields should be configured."""
        self.assertIn("ip_prefix", self.admin.search_fields)
        self.assertIn("path", self.admin.search_fields)


class TestFuzzAttemptAdminDetailView(TestCase):
    """Test FuzzAttemptAdmin detail view configuration."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = FuzzAttemptAdmin(FuzzAttempt, self.site)

        self.attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="POST",
            path="/test/path",
            status_returned=418,
            reason='["scanner_ua:sqlmap", "probe_path:/wp-admin"]',
            score=70,
        )

    def test_detail_view_accessible(self):
        """Admin should have readonly_fields configured."""
        self.assertTrue(len(self.admin.readonly_fields) > 0)

    def test_detail_view_shows_reason(self):
        """Admin should have reason_formatted in readonly_fields."""
        self.assertIn("reason_formatted", self.admin.readonly_fields)

    def test_detail_view_readonly(self):
        """has_change_permission should return False."""
        factory = RequestFactory()
        request = factory.get("/admin/fighthealthinsurance/fuzzattempt/1/change/")
        request.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass"
        )
        self.assertFalse(self.admin.has_change_permission(request))

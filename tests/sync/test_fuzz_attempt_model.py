"""
Tests for FuzzAttempt model functionality.
"""

import json

from django.test import TestCase, override_settings

from fighthealthinsurance.models import FuzzAttempt


class TestFuzzAttemptModel(TestCase):
    """Test FuzzAttempt model functionality."""

    def test_create_fuzz_attempt_basic(self):
        """Should create FuzzAttempt with required fields."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test_reason"]',
            score=100,
        )
        self.assertIsNotNone(attempt.id)
        self.assertIsNotNone(attempt.created_at)

    def test_ip_hash_is_stored(self):
        """IP hash should be stored."""
        ip_hash = "b" * 64
        attempt = FuzzAttempt.objects.create(
            ip_hash=ip_hash,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertEqual(attempt.ip_hash, ip_hash)

    def test_raw_ip_nullable(self):
        """Raw IP should be nullable by default."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="c" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertIsNone(attempt.raw_ip)

    def test_raw_ip_stored_when_provided(self):
        """Raw IP should be stored when provided."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="d" * 64,
            ip_prefix="192.168.1.0/24",
            raw_ip="192.168.1.100",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertEqual(attempt.raw_ip, "192.168.1.100")

    def test_ip_prefix_ipv4(self):
        """IPv4 prefix should be stored correctly."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="e" * 64,
            ip_prefix="10.0.0.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertEqual(attempt.ip_prefix, "10.0.0.0/24")

    def test_ip_prefix_ipv6(self):
        """IPv6 prefix should be stored correctly."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="f" * 64,
            ip_prefix="2001:db8::/64",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertEqual(attempt.ip_prefix, "2001:db8::/64")

    def test_reason_stores_json_list(self):
        """Reason field should store JSON list of triggered rules."""
        reasons = ["probe_path:/wp-admin", "scanner_ua:sqlmap"]
        attempt = FuzzAttempt.objects.create(
            ip_hash="g" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/wp-admin",
            status_returned=400,
            reason=json.dumps(reasons),
            score=70,
        )
        loaded_reasons = json.loads(attempt.reason)
        self.assertEqual(loaded_reasons, reasons)

    def test_user_association_nullable(self):
        """User FK should be nullable."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="h" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertIsNone(attempt.user)

    def test_session_key_stored(self):
        """Session key should be stored when provided."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="i" * 64,
            ip_prefix="192.168.1.0/24",
            session_key="test-session-key-12345",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertEqual(attempt.session_key, "test-session-key-12345")

    def test_key_version_default(self):
        """Key version should default to v1."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="j" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        self.assertEqual(attempt.key_version, "v1")

    def test_str_representation(self):
        """__str__ should return readable representation."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="k" * 64,
            ip_prefix="192.168.1.0/24",
            method="POST",
            path="/very/long/path/that/will/be/truncated/in/str/representation",
            status_returned=418,
            reason='["test"]',
            score=100,
        )
        str_rep = str(attempt)
        self.assertIn("POST", str_rep)
        self.assertIn("score=100", str_rep)

    def test_ordering_by_created_at_desc(self):
        """Records should be ordered by created_at descending."""
        attempt1 = FuzzAttempt.objects.create(
            ip_hash="l" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/first",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        attempt2 = FuzzAttempt.objects.create(
            ip_hash="m" * 64,
            ip_prefix="192.168.2.0/24",
            method="GET",
            path="/second",
            status_returned=400,
            reason='["test"]',
            score=50,
        )
        attempts = list(FuzzAttempt.objects.all())
        self.assertEqual(attempts[0].id, attempt2.id)  # Most recent first
        self.assertEqual(attempts[1].id, attempt1.id)  # Older record second

    def test_request_id_stored(self):
        """Request ID should be stored when provided."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="n" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["test"]',
            score=50,
            request_id="abc12345",
        )
        self.assertEqual(attempt.request_id, "abc12345")


class TestFuzzAttemptPermissions(TestCase):
    """Test FuzzAttempt model permissions."""

    def test_view_fuzz_capture_permission_exists(self):
        """Model should have view_fuzz_capture permission defined."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get_for_model(FuzzAttempt)
        permission = Permission.objects.filter(
            content_type=content_type,
            codename="view_fuzz_capture",
        ).first()
        self.assertIsNotNone(
            permission,
            "Expected 'view_fuzz_capture' Permission for model FuzzAttempt to be defined",
        )

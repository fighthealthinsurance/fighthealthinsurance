"""
Tests for the simplified audit logging system.
"""

from django.test import TestCase, RequestFactory, override_settings
from django.contrib.auth import get_user_model

from fhi_users.audit import (
    AuditLog,
    EventType,
    log_event,
    log_login_success,
    log_login_failure,
    log_logout,
    log_api_access,
    is_audit_enabled,
    get_client_ip,
)


User = get_user_model()


class AuditLoggingDisabledTest(TestCase):
    """Tests for when audit logging is disabled."""

    @override_settings(ENABLE_AUDIT_LOGGING=False)
    def test_is_audit_enabled_false(self):
        """Verify audit logging is disabled by default."""
        self.assertFalse(is_audit_enabled())

    @override_settings(ENABLE_AUDIT_LOGGING=False)
    def test_log_event_returns_none_when_disabled(self):
        """Log event returns None when disabled."""
        result = log_event(EventType.LOGIN_SUCCESS)
        self.assertIsNone(result)
        self.assertEqual(AuditLog.objects.count(), 0)


@override_settings(ENABLE_AUDIT_LOGGING=True)
class AuditLoggingEnabledTest(TestCase):
    """Tests for when audit logging is enabled."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

    def test_is_audit_enabled_true(self):
        """Verify audit logging can be enabled."""
        self.assertTrue(is_audit_enabled())

    def test_log_event_basic(self):
        """Test basic event logging."""
        log = log_event(EventType.LOGIN_SUCCESS, user=self.user)

        self.assertIsNotNone(log)
        self.assertEqual(log.event_type, "login_success")
        self.assertEqual(log.user, self.user)
        self.assertEqual(log.username, "testuser")

    def test_log_login_success(self):
        """Test login success logging."""
        request = self.factory.get("/")
        request.user = self.user

        log = log_login_success(request, self.user)

        self.assertIsNotNone(log)
        self.assertEqual(log.event_type, "login_success")

    def test_log_login_failure(self):
        """Test login failure logging."""
        request = self.factory.post("/login/")

        log = log_login_failure(request, username="baduser", reason="invalid_password")

        self.assertIsNotNone(log)
        self.assertEqual(log.event_type, "login_failed")
        self.assertIn("baduser", log.description)
        self.assertEqual(log.extra_data["attempted_username"], "baduser")

    def test_log_logout(self):
        """Test logout logging."""
        request = self.factory.get("/logout/")
        request.user = self.user

        log = log_logout(request, self.user)

        self.assertIsNotNone(log)
        self.assertEqual(log.event_type, "logout")

    def test_log_api_access(self):
        """Test API access logging."""
        request = self.factory.get("/api/v1/denials/")
        request.user = self.user

        log = log_api_access(request, status_code=200, response_time_ms=50)

        self.assertIsNotNone(log)
        self.assertEqual(log.event_type, "api_access")
        self.assertEqual(log.status_code, 200)
        self.assertEqual(log.response_time_ms, 50)
        self.assertEqual(log.path, "/api/v1/denials/")

    def test_log_event_with_description(self):
        """Test event with custom description."""
        log = log_event(
            EventType.SUSPICIOUS_ACTIVITY,
            description="Multiple failed attempts detected",
        )

        self.assertIsNotNone(log)
        self.assertEqual(log.description, "Multiple failed attempts detected")

    def test_log_event_with_extra_data(self):
        """Test event with extra JSON data."""
        log = log_event(
            EventType.API_ACCESS,
            extra_data={"endpoint": "/api/v1/test", "query_count": 5},
        )

        self.assertIsNotNone(log)
        self.assertEqual(log.extra_data["endpoint"], "/api/v1/test")
        self.assertEqual(log.extra_data["query_count"], 5)


class GetClientIPTest(TestCase):
    """Tests for IP extraction from requests."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_get_client_ip_x_forwarded_for(self):
        """Test IP extraction from X-Forwarded-For header."""
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8")
        ip = get_client_ip(request)
        self.assertEqual(ip, "1.2.3.4")

    def test_get_client_ip_x_real_ip(self):
        """Test IP extraction from X-Real-IP header."""
        request = self.factory.get("/", HTTP_X_REAL_IP="9.10.11.12")
        ip = get_client_ip(request)
        self.assertEqual(ip, "9.10.11.12")

    def test_get_client_ip_remote_addr(self):
        """Test IP extraction from REMOTE_ADDR."""
        request = self.factory.get("/")
        # RequestFactory sets REMOTE_ADDR to 127.0.0.1 by default
        ip = get_client_ip(request)
        self.assertEqual(ip, "127.0.0.1")

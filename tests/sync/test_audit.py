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
    get_user_agent,
    extract_tracking_info,
    extract_tracking_info_from_scope,
    TrackingInfo,
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


class TrackingInfoTest(TestCase):
    """Tests for tracking info extraction."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_get_user_agent(self):
        """Test user agent extraction from request."""
        request = self.factory.get(
            "/", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )
        ua = get_user_agent(request)
        self.assertEqual(ua, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

    def test_get_user_agent_empty(self):
        """Test user agent extraction when not present."""
        request = self.factory.get("/")
        ua = get_user_agent(request)
        self.assertEqual(ua, "")

    def test_get_user_agent_truncation(self):
        """Test user agent is truncated to 500 chars."""
        long_ua = "A" * 600
        request = self.factory.get("/", HTTP_USER_AGENT=long_ua)
        ua = get_user_agent(request)
        self.assertEqual(len(ua), 500)

    def test_extract_tracking_info_basic(self):
        """Test basic tracking info extraction."""
        request = self.factory.get(
            "/test/",
            HTTP_USER_AGENT="TestBrowser/1.0",
            HTTP_X_FORWARDED_FOR="192.168.1.1",
        )
        info = extract_tracking_info(request, is_professional=False)

        self.assertIsInstance(info, TrackingInfo)
        self.assertEqual(info.user_agent, "TestBrowser/1.0")
        # IP should be None for non-professional
        self.assertIsNone(info.ip_address)

    def test_extract_tracking_info_professional(self):
        """Test tracking info extraction for professional users includes IP."""
        request = self.factory.get(
            "/test/",
            HTTP_USER_AGENT="TestBrowser/1.0",
            HTTP_X_FORWARDED_FOR="192.168.1.1",
        )
        info = extract_tracking_info(request, is_professional=True)

        self.assertIsInstance(info, TrackingInfo)
        self.assertEqual(info.user_agent, "TestBrowser/1.0")
        # IP should be present for professional
        self.assertEqual(info.ip_address, "192.168.1.1")

    def test_extract_tracking_info_none_request(self):
        """Test tracking info extraction with None request."""
        info = extract_tracking_info(None)
        self.assertIsInstance(info, TrackingInfo)
        self.assertEqual(info.user_agent, "")
        self.assertIsNone(info.ip_address)
        self.assertEqual(info.asn, "")
        self.assertEqual(info.asn_name, "")


class TrackingInfoFromScopeTest(TestCase):
    """Tests for tracking info extraction from websocket scope."""

    def test_extract_tracking_info_from_scope_basic(self):
        """Test tracking info extraction from websocket scope."""
        scope = {
            "headers": [
                (b"user-agent", b"WebSocketClient/1.0"),
                (b"x-forwarded-for", b"10.0.0.1"),
            ],
            "client": ("127.0.0.1", 12345),
        }
        info = extract_tracking_info_from_scope(scope, is_professional=False)

        self.assertIsInstance(info, TrackingInfo)
        self.assertEqual(info.user_agent, "WebSocketClient/1.0")
        # IP should be None for non-professional
        self.assertIsNone(info.ip_address)

    def test_extract_tracking_info_from_scope_professional(self):
        """Test tracking info extraction for professional includes IP."""
        scope = {
            "headers": [
                (b"user-agent", b"WebSocketClient/1.0"),
                (b"x-forwarded-for", b"10.0.0.1"),
            ],
            "client": ("127.0.0.1", 12345),
        }
        info = extract_tracking_info_from_scope(scope, is_professional=True)

        self.assertEqual(info.user_agent, "WebSocketClient/1.0")
        self.assertEqual(info.ip_address, "10.0.0.1")

    def test_extract_tracking_info_from_scope_x_real_ip(self):
        """Test tracking info uses X-Real-IP when X-Forwarded-For not present."""
        scope = {
            "headers": [
                (b"user-agent", b"Test/1.0"),
                (b"x-real-ip", b"172.16.0.1"),
            ],
            "client": ("127.0.0.1", 12345),
        }
        info = extract_tracking_info_from_scope(scope, is_professional=True)

        self.assertEqual(info.ip_address, "172.16.0.1")

    def test_extract_tracking_info_from_scope_client_fallback(self):
        """Test tracking info uses client address when no headers present."""
        scope = {
            "headers": [],
            "client": ("192.168.1.100", 54321),
        }
        info = extract_tracking_info_from_scope(scope, is_professional=True)

        self.assertEqual(info.ip_address, "192.168.1.100")

    def test_extract_tracking_info_from_scope_none(self):
        """Test tracking info extraction with None scope."""
        info = extract_tracking_info_from_scope(None)
        self.assertIsInstance(info, TrackingInfo)
        self.assertEqual(info.user_agent, "")
        self.assertIsNone(info.ip_address)

    def test_tracking_info_to_model_kwargs(self):
        """Test conversion of TrackingInfo to model kwargs."""
        info = TrackingInfo(
            user_agent="TestAgent/1.0",
            ip_address="192.168.1.1",
            asn="AS12345",
            asn_name="Test ISP",
        )
        kwargs = info.to_model_kwargs()

        self.assertIsInstance(kwargs, dict)
        self.assertEqual(kwargs["user_agent"], "TestAgent/1.0")
        self.assertEqual(kwargs["ip_address"], "192.168.1.1")
        self.assertEqual(kwargs["asn"], "AS12345")
        self.assertEqual(kwargs["asn_name"], "Test ISP")

    def test_tracking_info_to_model_kwargs_defaults(self):
        """Test conversion with default values."""
        info = TrackingInfo()
        kwargs = info.to_model_kwargs()

        self.assertEqual(kwargs["user_agent"], "")
        self.assertIsNone(kwargs["ip_address"])
        self.assertEqual(kwargs["asn"], "")
        self.assertEqual(kwargs["asn_name"], "")

    def test_tracking_info_update_model_fields(self):
        """Test updating model instance fields with tracking info."""
        # Create a mock model instance
        class MockModel:
            def __init__(self):
                self.user_agent = ""
                self.asn = ""
                self.asn_name = ""
                self.ip_address = None

        info = TrackingInfo(
            user_agent="TestAgent/2.0",
            ip_address="10.0.0.1",
            asn="AS9999",
            asn_name="Test ASN",
        )
        model = MockModel()
        info.update_model_fields(model)

        self.assertEqual(model.user_agent, "TestAgent/2.0")
        self.assertEqual(model.ip_address, "10.0.0.1")
        self.assertEqual(model.asn, "AS9999")
        self.assertEqual(model.asn_name, "Test ASN")


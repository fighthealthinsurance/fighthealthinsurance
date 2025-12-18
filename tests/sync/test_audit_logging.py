"""Tests for audit logging functionality."""

import pytest
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model

from fhi_users.audit_models import (
    AuditEventType,
    NetworkType,
    UserType,
    KNOWN_DATACENTER_ASNS,
    KNOWN_VPN_ASNS,
)
from fhi_users.audit_utils import (
    classify_network_type,
    parse_user_agent_string,
    get_client_ip,
    _basic_ua_parse,
)


User = get_user_model()


class TestNetworkTypeClassification(TestCase):
    """Tests for ASN-based network type classification."""

    def test_known_datacenter_asns(self):
        """Known datacenter ASNs should be classified as DATACENTER."""
        # AWS
        self.assertEqual(
            classify_network_type(16509, "Amazon.com"),
            NetworkType.DATACENTER,
        )
        # Google Cloud
        self.assertEqual(
            classify_network_type(15169, "Google LLC"),
            NetworkType.DATACENTER,
        )

    def test_known_vpn_asns(self):
        """Known VPN ASNs should be classified as VPN."""
        self.assertEqual(
            classify_network_type(62041, "NordVPN"),
            NetworkType.VPN,
        )

    def test_heuristic_datacenter_detection(self):
        """ASNs with datacenter keywords should be detected."""
        self.assertEqual(
            classify_network_type(99999, "Example Hosting Company"),
            NetworkType.DATACENTER,
        )
        self.assertEqual(
            classify_network_type(99999, "Cloud Services Inc"),
            NetworkType.DATACENTER,
        )

    def test_heuristic_residential_detection(self):
        """Common ISPs should be classified as RESIDENTIAL."""
        self.assertEqual(
            classify_network_type(99999, "Comcast Cable Communications"),
            NetworkType.RESIDENTIAL,
        )
        self.assertEqual(
            classify_network_type(99999, "Verizon Online LLC"),
            NetworkType.RESIDENTIAL,
        )

    def test_heuristic_mobile_detection(self):
        """Mobile carriers should be classified as MOBILE."""
        self.assertEqual(
            classify_network_type(99999, "T-Mobile USA"),
            NetworkType.MOBILE,
        )
        self.assertEqual(
            classify_network_type(99999, "Verizon Wireless"),
            NetworkType.MOBILE,
        )

    def test_unknown_asn(self):
        """Unknown ASNs should be classified as UNKNOWN."""
        self.assertEqual(
            classify_network_type(99999, "Some Random Company"),
            NetworkType.UNKNOWN,
        )
        self.assertEqual(
            classify_network_type(None, None),
            NetworkType.UNKNOWN,
        )


class TestUserAgentParsing(TestCase):
    """Tests for user agent string parsing."""

    def test_chrome_windows_detection(self):
        """Chrome on Windows should be detected."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        info = parse_user_agent_string(ua)
        self.assertEqual(info.browser_family, "Chrome")
        self.assertEqual(info.os_family, "Windows")
        self.assertFalse(info.is_mobile)
        self.assertFalse(info.is_bot)

    def test_safari_ios_detection(self):
        """Safari on iOS should be detected as mobile."""
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        info = parse_user_agent_string(ua)
        self.assertEqual(info.browser_family, "Mobile Safari")
        self.assertEqual(info.os_family, "iOS")
        self.assertTrue(info.is_mobile)

    def test_bot_detection(self):
        """Bot user agents should be detected."""
        ua = "Googlebot/2.1 (+http://www.google.com/bot.html)"
        info = parse_user_agent_string(ua)
        self.assertTrue(info.is_bot)

    def test_empty_user_agent(self):
        """Empty user agent should not crash."""
        info = parse_user_agent_string("")
        self.assertEqual(info.full_user_agent, "")
        self.assertFalse(info.is_bot)

    def test_none_user_agent(self):
        """None user agent should not crash."""
        info = parse_user_agent_string(None)
        self.assertEqual(info.full_user_agent, "")

    def test_simplified_output(self):
        """Simplified output should return browser/OS."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0"
        info = parse_user_agent_string(ua)
        simplified = info.simplified
        self.assertIn("Chrome", simplified)
        self.assertIn("Windows", simplified)


class TestBasicUAParse(TestCase):
    """Tests for basic user agent parsing fallback."""

    def test_firefox_detection(self):
        """Firefox should be detected."""
        ua = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"
        info = _basic_ua_parse(ua)
        self.assertEqual(info.browser_family, "Firefox")
        self.assertEqual(info.os_family, "Linux")

    def test_android_mobile_detection(self):
        """Android should be detected as mobile."""
        ua = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile"
        info = _basic_ua_parse(ua)
        self.assertEqual(info.os_family, "Android")
        self.assertTrue(info.is_mobile)

    def test_curl_bot_detection(self):
        """curl should be detected as bot."""
        ua = "curl/7.88.1"
        info = _basic_ua_parse(ua)
        self.assertTrue(info.is_bot)

    def test_python_requests_bot_detection(self):
        """Python requests should be detected as bot."""
        ua = "python-requests/2.31.0"
        info = _basic_ua_parse(ua)
        self.assertTrue(info.is_bot)


class TestGetClientIP(TestCase):
    """Tests for client IP extraction from requests."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_x_forwarded_for_single(self):
        """X-Forwarded-For with single IP."""
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="203.0.113.195")
        self.assertEqual(get_client_ip(request), "203.0.113.195")

    def test_x_forwarded_for_chain(self):
        """X-Forwarded-For with multiple IPs takes first."""
        request = self.factory.get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.195, 70.41.3.18, 150.172.238.178"
        )
        self.assertEqual(get_client_ip(request), "203.0.113.195")

    def test_x_real_ip(self):
        """X-Real-IP header should be used."""
        request = self.factory.get("/", HTTP_X_REAL_IP="203.0.113.195")
        self.assertEqual(get_client_ip(request), "203.0.113.195")

    def test_remote_addr_fallback(self):
        """REMOTE_ADDR should be used as fallback."""
        request = self.factory.get("/")
        # RequestFactory sets REMOTE_ADDR to 127.0.0.1 by default
        self.assertEqual(get_client_ip(request), "127.0.0.1")


class TestAuditEventTypes(TestCase):
    """Tests for audit event type enum."""

    def test_all_event_types_have_values(self):
        """All event types should have string values."""
        for event_type in AuditEventType:
            self.assertIsInstance(event_type.value, str)
            self.assertGreater(len(event_type.value), 0)

    def test_event_types_are_unique(self):
        """All event type values should be unique."""
        values = [e.value for e in AuditEventType]
        self.assertEqual(len(values), len(set(values)))


class TestKnownASNLists(TestCase):
    """Tests for the known ASN lists."""

    def test_datacenter_asns_are_integers(self):
        """All datacenter ASNs should be integers."""
        for asn in KNOWN_DATACENTER_ASNS:
            self.assertIsInstance(asn, int)
            self.assertGreater(asn, 0)

    def test_vpn_asns_are_integers(self):
        """All VPN ASNs should be integers."""
        for asn in KNOWN_VPN_ASNS:
            self.assertIsInstance(asn, int)
            self.assertGreater(asn, 0)

    def test_common_cloud_providers_included(self):
        """Major cloud providers should be in datacenter list."""
        # AWS
        self.assertIn(16509, KNOWN_DATACENTER_ASNS)
        # Google
        self.assertIn(15169, KNOWN_DATACENTER_ASNS)
        # Azure
        self.assertIn(8075, KNOWN_DATACENTER_ASNS)


class TestAuditServiceLogObjectActivity(TestCase):
    """Tests for audit_service.log_object_activity()."""

    def setUp(self):
        """
        Prepare a RequestFactory and create a reusable test user for each test.
        
        The created user has username "testuser", email "test@example.com", and password "testpass123", and is available as self.user for test cases. The RequestFactory instance is available as self.factory.
        """
        self.factory = RequestFactory()
        # Create a test user
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

    def tearDown(self):
        # Clean up
        """
        Remove ObjectActivityContext entries created during the test run and delete the test user.
        
        This ensures database state is cleaned up after each test by deleting all ObjectActivityContext records and removing the user created in setUp.
        """
        from fhi_users.audit_models import ObjectActivityContext
        ObjectActivityContext.objects.all().delete()
        self.user.delete()

    def test_log_object_activity_with_django_request(self):
        """log_object_activity should work with Django HttpRequest."""
        from fhi_users.audit_service import audit_service
        from fhi_users.audit_models import ObjectActivityContext
        from fighthealthinsurance.models import Denial

        # Create a test denial
        denial = Denial.objects.create(
            denial_text="Test denial",
            hashed_email="test@example.com",
        )

        # Create a Django request
        request = self.factory.post("/test/")
        request.user = self.user
        request.session = {}

        # Log the activity
        result = audit_service.log_object_activity(
            request=request,
            obj=denial,
            action="create",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "create")
        self.assertEqual(result.object_id, str(denial.pk))

        # Verify we can retrieve it
        contexts = ObjectActivityContext.objects.filter(
            object_id=str(denial.pk)
        )
        self.assertEqual(contexts.count(), 1)

        # Cleanup
        denial.delete()

    def test_log_object_activity_with_drf_request(self):
        """log_object_activity should work with DRF Request."""
        from rest_framework.request import Request
        from fhi_users.audit_service import audit_service
        from fhi_users.audit_models import ObjectActivityContext
        from fighthealthinsurance.models import Denial

        # Create a test denial
        denial = Denial.objects.create(
            denial_text="Test denial for DRF",
            hashed_email="drf@example.com",
        )

        # Create a Django request and wrap in DRF Request
        django_request = self.factory.post("/api/test/")
        django_request.user = self.user
        django_request.session = {}
        drf_request = Request(django_request)

        # Log the activity
        result = audit_service.log_object_activity(
            request=drf_request,
            obj=denial,
            action="create",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "create")
        self.assertEqual(result.object_id, str(denial.pk))

        # Cleanup
        denial.delete()

    def test_log_object_activity_update_action(self):
        """log_object_activity should correctly log update actions."""
        from fhi_users.audit_service import audit_service
        from fhi_users.audit_models import ObjectActivityContext
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            denial_text="Test denial",
            hashed_email="update@example.com",
        )

        request = self.factory.post("/test/")
        request.user = self.user
        request.session = {}

        # Log as update
        result = audit_service.log_object_activity(
            request=request,
            obj=denial,
            action="update",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "update")

        # Cleanup
        denial.delete()

    def test_log_object_activity_anonymous_user(self):
        """log_object_activity should work with anonymous users."""
        from django.contrib.auth.models import AnonymousUser
        from fhi_users.audit_service import audit_service
        from fhi_users.audit_models import ObjectActivityContext, UserType
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            denial_text="Test denial anonymous",
            hashed_email="anon@example.com",
        )

        request = self.factory.post("/test/")
        request.user = AnonymousUser()
        request.session = {}

        result = audit_service.log_object_activity(
            request=request,
            obj=denial,
            action="create",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.user_type, UserType.ANONYMOUS.value)
        self.assertIsNone(result.user)

        # Cleanup
        denial.delete()


class TestPrivacySanitization(TestCase):
    """Tests for privacy-based data sanitization."""

    def test_consumer_ip_not_stored(self):
        """Consumer users should not have IP address stored."""
        from fhi_users.audit_utils import (
            sanitize_for_privacy,
            IPInfo,
            UserAgentInfo,
        )
        from fhi_users.audit_models import UserType, NetworkType

        context = {
            "ip_info": IPInfo(
                ip_address="192.168.1.1",
                asn_number=12345,
                asn_org="Test ISP",
                country_code="US",
                state_region="California",
                network_type=NetworkType.RESIDENTIAL,
            ),
            "ua_info": UserAgentInfo(
                full_user_agent="Mozilla/5.0 Chrome/120.0",
                browser_family="Chrome",
                os_family="Windows",
            ),
            "session_key": "abc123",
        }

        sanitized = sanitize_for_privacy(context, UserType.CONSUMER)

        # IP should be empty for consumers
        self.assertEqual(sanitized["ip_info"].ip_address, "")
        # ASN should still be present
        self.assertEqual(sanitized["ip_info"].asn_number, 12345)
        self.assertEqual(sanitized["ip_info"].asn_org, "Test ISP")
        # Country should be present, but not state
        self.assertEqual(sanitized["ip_info"].country_code, "US")
        self.assertIsNone(sanitized["ip_info"].state_region)
        # Full UA should be empty
        self.assertEqual(sanitized["ua_info"].full_user_agent, "")
        # But browser/OS family should be present
        self.assertEqual(sanitized["ua_info"].browser_family, "Chrome")

    def test_professional_full_data_stored(self):
        """Professional users should have full data stored."""
        from fhi_users.audit_utils import (
            sanitize_for_privacy,
            IPInfo,
            UserAgentInfo,
        )
        from fhi_users.audit_models import UserType, NetworkType

        context = {
            "ip_info": IPInfo(
                ip_address="192.168.1.1",
                asn_number=12345,
                asn_org="Test ISP",
                country_code="US",
                state_region="California",
                network_type=NetworkType.RESIDENTIAL,
            ),
            "ua_info": UserAgentInfo(
                full_user_agent="Mozilla/5.0 Chrome/120.0",
                browser_family="Chrome",
                os_family="Windows",
            ),
            "session_key": "abc123",
        }

        sanitized = sanitize_for_privacy(context, UserType.PROFESSIONAL)

        # Professional should have full data
        self.assertEqual(sanitized["ip_info"].ip_address, "192.168.1.1")
        self.assertEqual(sanitized["ip_info"].state_region, "California")
        self.assertEqual(
            sanitized["ua_info"].full_user_agent, "Mozilla/5.0 Chrome/120.0"
        )


class TestDenialCreationAuditLogging(TestCase):
    """Integration tests for audit logging during denial creation."""

    def setUp(self):
        """
        Prepare test fixtures: initialize a RequestFactory and create a persistent test user.
        
        Creates:
        - self.factory: a Django RequestFactory instance for building test requests.
        - self.user: a User created with username "testuser2", email "testdenial@example.com", and password "testpass123".
        """
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="testuser2",
            email="testdenial@example.com",
            password="testpass123",
        )

    def tearDown(self):
        """
        Clean up test artifacts created by the test case.
        
        Deletes all ObjectActivityContext records, removes Denial records whose hashed_email starts with "integrationtest", and deletes the test user stored on self.user.
        """
        from fhi_users.audit_models import ObjectActivityContext
        from fighthealthinsurance.models import Denial
        ObjectActivityContext.objects.all().delete()
        Denial.objects.filter(hashed_email__startswith="integrationtest").delete()
        self.user.delete()

    def test_create_denial_logs_create_action(self):
        """Creating a new denial should log action='create'."""
        from fighthealthinsurance.common_view_logic import DenialCreatorHelper
        from fhi_users.audit_models import ObjectActivityContext

        request = self.factory.post("/test/")
        request.user = self.user
        request.session = {}

        # Create a denial through the helper
        result = DenialCreatorHelper.create_or_update_denial(
            email="integrationtest-create@example.com",
            denial_text="Test denial text for create",
            zip="94102",
            request=request,
        )

        # Check that audit log was created with action='create'
        contexts = ObjectActivityContext.objects.filter(
            object_id=str(result.denial_id)
        )
        self.assertEqual(contexts.count(), 1)
        self.assertEqual(contexts.first().action, "create")

    def test_update_denial_logs_update_action(self):
        """Updating an existing denial should log action='update'."""
        from fighthealthinsurance.common_view_logic import DenialCreatorHelper
        from fighthealthinsurance.models import Denial
        from fhi_users.audit_models import ObjectActivityContext

        # First create a denial directly
        denial = Denial.objects.create(
            denial_text="Original text",
            hashed_email="integrationtest-update@example.com",
        )

        request = self.factory.post("/test/")
        request.user = self.user
        request.session = {}

        # Update it through the helper
        result = DenialCreatorHelper.create_or_update_denial(
            email="integrationtest-update@example.com",
            denial_text="Updated text",
            zip="94102",
            denial=denial,  # Pass existing denial
            request=request,
        )

        # Check that audit log was created with action='update'
        contexts = ObjectActivityContext.objects.filter(
            object_id=str(denial.denial_id)
        )
        self.assertEqual(contexts.count(), 1)
        self.assertEqual(contexts.first().action, "update")

    def test_denial_creation_without_request_no_audit_log(self):
        """Creating a denial without request should not create audit log."""
        from fighthealthinsurance.common_view_logic import DenialCreatorHelper
        from fhi_users.audit_models import ObjectActivityContext

        # Create denial without passing request
        result = DenialCreatorHelper.create_or_update_denial(
            email="integrationtest-norequest@example.com",
            denial_text="Test denial no request",
            zip="94102",
            # No request parameter
        )

        # Check that no audit log was created
        contexts = ObjectActivityContext.objects.filter(
            object_id=str(result.denial_id)
        )
        self.assertEqual(contexts.count(), 0)
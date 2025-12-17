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

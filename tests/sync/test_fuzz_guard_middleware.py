"""
Tests for FuzzGuardMiddleware scoring, rate limiting, and responses.
"""

import json
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings

from fighthealthinsurance.human_verification import TEAPOT_MESSAGE
from fighthealthinsurance.middleware.FuzzGuardMiddleware import (
    DENIAL_ID_PATTERN,
    PROBE_PATH_PATTERNS,
    SCANNER_UA_REGEX,
    SQL_INJECTION_REGEX,
    determine_status_code,
    check_rate_limit,
    extract_denial_id,
    generate_fuzz_response,
    get_client_ip,
    get_ip_prefix,
    hash_ip,
    score_request,
)


class TestHelperFunctions(TestCase):
    """Test helper functions for IP handling and denial ID extraction."""

    def test_get_client_ip_from_remote_addr(self):
        """Should get IP from REMOTE_ADDR when no forwarding headers."""
        factory = RequestFactory()
        request = factory.get("/")
        request.META["REMOTE_ADDR"] = "192.168.1.1"
        self.assertEqual(get_client_ip(request), "192.168.1.1")

    def test_get_client_ip_from_x_forwarded_for(self):
        """Should get first IP from X-Forwarded-For header."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_X_FORWARDED_FOR="10.0.0.1, 10.0.0.2")
        self.assertEqual(get_client_ip(request), "10.0.0.1")

    def test_get_client_ip_from_x_real_ip(self):
        """Should get IP from X-Real-IP header."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_X_REAL_IP="172.16.0.1")
        self.assertEqual(get_client_ip(request), "172.16.0.1")

    def test_hash_ip_consistent(self):
        """Same IP and salt should produce same hash."""
        hash1 = hash_ip("192.168.1.1", "test-salt")
        hash2 = hash_ip("192.168.1.1", "test-salt")
        self.assertEqual(hash1, hash2)

    def test_hash_ip_different_salt(self):
        """Different salt should produce different hash."""
        hash1 = hash_ip("192.168.1.1", "salt-1")
        hash2 = hash_ip("192.168.1.1", "salt-2")
        self.assertNotEqual(hash1, hash2)

    def test_get_ip_prefix_ipv4(self):
        """IPv4 addresses should get /24 prefix."""
        prefix = get_ip_prefix("192.168.1.100")
        self.assertEqual(prefix, "192.168.1.0/24")

    def test_get_ip_prefix_ipv6(self):
        """IPv6 addresses should get /64 prefix."""
        prefix = get_ip_prefix("2001:db8::1")
        self.assertEqual(prefix, "2001:db8::/64")

    def test_get_ip_prefix_invalid(self):
        """Invalid IP should return 'invalid'."""
        prefix = get_ip_prefix("not-an-ip")
        self.assertEqual(prefix, "invalid")

    def test_extract_denial_id_from_get(self):
        """Should extract denial_id from query params."""
        factory = RequestFactory()
        request = factory.get("/?denial_id=123")
        self.assertEqual(extract_denial_id(request), "123")

    def test_extract_denial_id_from_post(self):
        """Should extract denial_id from POST data."""
        factory = RequestFactory()
        request = factory.post("/", {"denial_id": "456"})
        self.assertEqual(extract_denial_id(request), "456")

    def test_extract_denial_id_missing(self):
        """Should return None when no denial_id."""
        factory = RequestFactory()
        request = factory.get("/")
        self.assertIsNone(extract_denial_id(request))


class TestDenialIdPattern(TestCase):
    """Test denial ID validation pattern."""

    def test_valid_positive_integer(self):
        """Positive integers should be valid."""
        self.assertTrue(DENIAL_ID_PATTERN.match("123"))
        self.assertTrue(DENIAL_ID_PATTERN.match("0"))
        self.assertTrue(DENIAL_ID_PATTERN.match("999999"))

    def test_valid_negative_integer(self):
        """Negative integers should be valid."""
        self.assertTrue(DENIAL_ID_PATTERN.match("-1"))
        self.assertTrue(DENIAL_ID_PATTERN.match("-999"))

    def test_invalid_non_numeric(self):
        """Non-numeric strings should be invalid."""
        self.assertFalse(DENIAL_ID_PATTERN.match("abc"))
        self.assertFalse(DENIAL_ID_PATTERN.match("12abc"))
        self.assertFalse(DENIAL_ID_PATTERN.match("<script>"))

    def test_invalid_float(self):
        """Floats should be invalid."""
        self.assertFalse(DENIAL_ID_PATTERN.match("12.34"))

    def test_invalid_special_chars(self):
        """Special characters should be invalid."""
        self.assertFalse(DENIAL_ID_PATTERN.match("1;DROP TABLE"))
        self.assertFalse(DENIAL_ID_PATTERN.match("' OR '1'='1"))


class TestScoringDenialId(TestCase):
    """Test scoring for invalid denial IDs."""

    def test_invalid_denial_id_query_param_triggers_block(self):
        """denial_id=abc in query param should score +100."""
        factory = RequestFactory()
        request = factory.get("/?denial_id=abc")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 100)
        self.assertTrue(any("invalid_denial_id" in r for r in reasons))

    def test_invalid_denial_id_post_data_triggers_block(self):
        """denial_id=<script> in POST should score +100."""
        factory = RequestFactory()
        request = factory.post("/", {"denial_id": "<script>alert(1)</script>"})
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 100)
        self.assertTrue(any("invalid_denial_id" in r for r in reasons))

    def test_valid_integer_denial_id_passes(self):
        """denial_id=123 should not add to score."""
        factory = RequestFactory()
        request = factory.get("/?denial_id=123")
        score, reasons = score_request(request)
        self.assertFalse(any("invalid_denial_id" in r for r in reasons))

    def test_negative_denial_id_is_valid(self):
        """denial_id=-1 is technically valid integer format."""
        factory = RequestFactory()
        request = factory.get("/?denial_id=-1")
        score, reasons = score_request(request)
        self.assertFalse(any("invalid_denial_id" in r for r in reasons))

    def test_empty_denial_id_does_not_trigger(self):
        """Empty denial_id should not trigger invalid_denial_id rule."""
        factory = RequestFactory()
        request = factory.get("/?denial_id=")
        score, reasons = score_request(request)
        self.assertFalse(any("invalid_denial_id" in r for r in reasons))


class TestScoringUserAgent(TestCase):
    """Test scoring for User-Agent patterns."""

    def test_scanner_ua_sqlmap_triggers_block(self):
        """User-Agent containing sqlmap should score +40."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_USER_AGENT="sqlmap/1.4.7")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 40)
        self.assertTrue(any("scanner_ua" in r for r in reasons))

    def test_scanner_ua_nikto_triggers_block(self):
        """User-Agent containing nikto should score +40."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_USER_AGENT="Nikto/2.1.6")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 40)

    def test_scanner_ua_ffuf_triggers_block(self):
        """User-Agent containing ffuf should score +40."""
        factory = RequestFactory()
        # ffuf typically reports as "ffuf/1.x.x" or similar
        request = factory.get("/", HTTP_USER_AGENT="ffuf/1.3.1-dev")
        score, reasons = score_request(request)
        # ffuf matches the scanner pattern
        self.assertGreaterEqual(score, 40, "Scanner UA should trigger score >= 40")
        self.assertTrue(
            any("scanner" in r.lower() for r in reasons),
            f"Expected scanner-related reason in {reasons}",
        )

    def test_empty_user_agent_adds_score(self):
        """Empty User-Agent should add +10 score."""
        factory = RequestFactory()
        request = factory.get("/")
        request.META.pop("HTTP_USER_AGENT", None)
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 10)
        self.assertTrue(any("empty_user_agent" in r for r in reasons))

    def test_normal_browser_ua_passes(self):
        """Normal browser UA should not add scanner score."""
        factory = RequestFactory()
        request = factory.get(
            "/",
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        score, reasons = score_request(request)
        self.assertFalse(any("scanner_ua" in r for r in reasons))


class TestScoringProbePaths(TestCase):
    """Test scoring for probe paths."""

    def test_probe_path_wp_admin(self):
        """Request to /wp-admin should score +30."""
        factory = RequestFactory()
        request = factory.get("/wp-admin/")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 30)
        self.assertTrue(any("probe_path" in r for r in reasons))

    def test_probe_path_env(self):
        """Request to /.env should score +30."""
        factory = RequestFactory()
        request = factory.get("/.env")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 30)
        self.assertTrue(any("probe_path" in r for r in reasons))

    def test_probe_path_phpmyadmin(self):
        """Request to /phpmyadmin should score +30."""
        factory = RequestFactory()
        request = factory.get("/phpmyadmin/")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 30)

    def test_probe_path_actuator(self):
        """Request to /actuator should score +30."""
        factory = RequestFactory()
        request = factory.get("/actuator/health")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 30)


class TestScoringHttpMethods(TestCase):
    """Test scoring for suspicious HTTP methods."""

    def test_suspicious_method_trace(self):
        """TRACE method should score +50."""
        factory = RequestFactory()
        request = factory.generic("TRACE", "/")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 50)
        self.assertTrue(any("suspicious_method" in r for r in reasons))

    def test_suspicious_method_connect(self):
        """CONNECT method should score +50."""
        factory = RequestFactory()
        request = factory.generic("CONNECT", "/")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 50)

    def test_normal_methods_pass(self):
        """GET, POST, PUT, DELETE should not add method score."""
        factory = RequestFactory()
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            request = factory.generic(method, "/")
            score, reasons = score_request(request)
            self.assertFalse(any("suspicious_method" in r for r in reasons))


class TestScoringLengthLimits(TestCase):
    """Test scoring for excessive lengths."""

    def test_excessive_path_length(self):
        """Path > 1000 chars should score +20."""
        factory = RequestFactory()
        long_path = "/" + "a" * 1001
        request = factory.get(long_path)
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 20)
        self.assertTrue(any("excessive_path_length" in r for r in reasons))

    def test_excessive_querystring_length(self):
        """Query string > 2000 chars should score +20."""
        factory = RequestFactory()
        long_query = "x=" + "a" * 2001
        request = factory.get("/?" + long_query)
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 20)
        self.assertTrue(any("excessive_querystring_length" in r for r in reasons))

    def test_excessive_query_params(self):
        """More than 20 query params should score +15."""
        factory = RequestFactory()
        params = "&".join(f"param{i}=value{i}" for i in range(25))
        request = factory.get("/?" + params)
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 15)
        self.assertTrue(any("excessive_query_params" in r for r in reasons))


class TestScoringAttackPatterns(TestCase):
    """Test scoring for attack patterns."""

    def test_sql_injection_union_select(self):
        """SQL injection pattern 'union select' should score +60."""
        factory = RequestFactory()
        request = factory.get("/?q=1 UNION SELECT * FROM users")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 60)
        self.assertTrue(any("sql_injection" in r for r in reasons))

    def test_path_traversal_dotdot(self):
        """Path containing '..' should score +50."""
        factory = RequestFactory()
        request = factory.get("/../../../etc/passwd")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 50)
        self.assertTrue(any("path_traversal" in r for r in reasons))

    def test_path_traversal_encoded(self):
        """Path containing '%2e%2e' should score +50."""
        factory = RequestFactory()
        request = factory.get("/%2e%2e/%2e%2e/etc/passwd")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 50)


class TestScoringCombined(TestCase):
    """Test combined scoring scenarios."""

    def test_combined_score_threshold(self):
        """Multiple signals should combine and trigger when >= threshold."""
        factory = RequestFactory()
        # Empty UA (+10) + scanner path (+30) = 40 (below default 50)
        request = factory.get("/wp-admin/")
        request.META.pop("HTTP_USER_AGENT", None)
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 40)

    def test_below_threshold_passes(self):
        """Score below threshold should pass request through."""
        factory = RequestFactory()
        request = factory.get(
            "/",
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        )
        score, reasons = score_request(request)
        self.assertLess(score, 50)  # Default threshold


class TestRateLimiting(TestCase):
    """Test rate limiting functionality."""

    def test_rate_limit_triggers_after_threshold(self):
        """Exceeding rate limit should return is_throttled=True."""
        # Test the rate limit logic directly with a mock cache
        with patch(
            "fighthealthinsurance.middleware.FuzzGuardMiddleware.cache"
        ) as mock_cache:
            # Simulate a counter that increments
            counter = {"value": 0}

            def mock_add(key, value, timeout=None):
                if counter["value"] == 0:
                    counter["value"] = value
                    return True
                return False

            def mock_incr(key):
                counter["value"] += 1
                return counter["value"]

            mock_cache.add.side_effect = mock_add
            mock_cache.incr.side_effect = mock_incr

            with override_settings(FUZZ_GUARD_RATE_LIMIT_PER_MINUTE=5):
                ip_hash = "test-rate-limit-hash"

                # First 5 requests should be OK
                for i in range(5):
                    is_throttled, count = check_rate_limit(ip_hash)
                    self.assertFalse(
                        is_throttled, f"Request {i+1} should not be throttled"
                    )

                # 6th request should be throttled
                is_throttled, count = check_rate_limit(ip_hash)
                self.assertTrue(
                    is_throttled, f"Request 6 should be throttled, count={count}"
                )
                self.assertEqual(count, 6)

    def test_different_ips_have_separate_limits(self):
        """Different IPs should have independent rate limits."""
        # Test with mock cache that tracks different keys
        with patch(
            "fighthealthinsurance.middleware.FuzzGuardMiddleware.cache"
        ) as mock_cache:
            counters = {}

            def mock_add(key, value, timeout=None):
                if key in counters:
                    return False
                counters[key] = value
                return True

            def mock_incr(key):
                counters[key] += 1
                return counters[key]

            mock_cache.add.side_effect = mock_add
            mock_cache.incr.side_effect = mock_incr

            ip_hash_1 = "test-ip-1"
            ip_hash_2 = "test-ip-2"

            # Hit limit for IP 1
            for _ in range(61):
                check_rate_limit(ip_hash_1)

            # IP 2 should still be OK
            is_throttled, count = check_rate_limit(ip_hash_2)
            self.assertFalse(is_throttled)
            self.assertEqual(count, 1)


class TestDetermineStatusCode(TestCase):
    """Test status code determination."""

    def test_throttled_returns_429(self):
        """Rate-limited requests should always return 429."""
        status = determine_status_code(is_throttled=True, teapot_prob=1.0)
        self.assertEqual(status, 429)

    def test_teapot_prob_1_returns_418(self):
        """With teapot_prob=1.0, should return 418."""
        status = determine_status_code(is_throttled=False, teapot_prob=1.0)
        self.assertEqual(status, 418)

    def test_teapot_prob_0_returns_400(self):
        """With teapot_prob=0, should return 400."""
        status = determine_status_code(is_throttled=False, teapot_prob=0)
        self.assertEqual(status, 400)

    def test_429_takes_precedence_over_teapot(self):
        """Rate-limited requests should return 429 even with teapot_prob=1.0."""
        status = determine_status_code(is_throttled=True, teapot_prob=1.0)
        self.assertEqual(status, 429)


class TestResponseGeneration(TestCase):
    """Test response generation."""

    def test_blocked_response_is_plain_text(self):
        """Blocked response should be text/plain."""
        response = generate_fuzz_response(status_code=400)
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_blocked_response_contains_message(self):
        """Blocked response should contain the friendly message."""
        response = generate_fuzz_response(status_code=400)
        self.assertEqual(response.content.decode("utf-8"), TEAPOT_MESSAGE)

    def test_response_uses_provided_status_code(self):
        """Response should use the explicit status code."""
        for code in [400, 418, 429]:
            response = generate_fuzz_response(status_code=code)
            self.assertEqual(response.status_code, code)

    def test_429_includes_retry_after_header(self):
        """429 response should include Retry-After: 60 header."""
        response = generate_fuzz_response(status_code=429, is_throttled=True)
        self.assertEqual(response.get("Retry-After"), "60")

    def test_non_throttled_no_retry_after(self):
        """Non-throttled response should not include Retry-After header."""
        response = generate_fuzz_response(status_code=400)
        self.assertIsNone(response.get("Retry-After"))

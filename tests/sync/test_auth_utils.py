"""Tests for authentication utility functions."""

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase, override_settings

from fhi_users.auth.auth_utils import (
    get_domain_id_from_request,
    validate_username,
    validate_password,
    normalize_phone_number,
    generic_validate_phone_number,
    validate_redirect_url,
    _sanitize_url_for_logging,
    _get_allowed_redirect_domains,
)
from rest_framework.serializers import ValidationError


class TestValidateUsername(TestCase):
    """Test username validation."""

    def test_valid_username(self):
        """Valid usernames should pass."""
        self.assertTrue(validate_username("john@example.com"))
        self.assertTrue(validate_username("user123"))
        self.assertTrue(validate_username("test_user"))

    def test_invalid_username_with_panda(self):
        """Usernames containing panda emoji should fail."""
        self.assertFalse(validate_username("user🐼domain"))
        self.assertFalse(validate_username("🐼"))


class TestValidatePassword(TestCase):
    """Test password validation using Django validators."""

    def test_valid_password(self):
        """Strong passwords should pass."""
        self.assertTrue(validate_password("SecureP@ss123"))
        self.assertTrue(validate_password("MyStr0ngP@ssword!"))

    def test_short_password(self):
        """Short passwords should fail."""
        self.assertFalse(validate_password("short"))
        self.assertFalse(validate_password("1234567"))

    def test_entirely_numeric_password(self):
        """All-numeric passwords should fail."""
        self.assertFalse(validate_password("12345678"))
        self.assertFalse(validate_password("9876543210"))

    def test_no_digit_password(self):
        """Passwords without digits should fail."""
        self.assertFalse(validate_password("password"))
        self.assertFalse(validate_password("NoDigitsHere"))


class TestNormalizePhoneNumber(TestCase):
    """Test phone number normalization."""

    def test_none_input(self):
        """None should return None."""
        self.assertIsNone(normalize_phone_number(None))

    def test_valid_phone_number(self):
        """Valid phone numbers should be cleaned."""
        self.assertEqual(normalize_phone_number("555-123-4567"), "5551234567")
        self.assertEqual(normalize_phone_number("(555) 123-4567"), "5551234567")
        # Leading 1 kept for 11-digit numbers (only stripped when > 11 digits)
        self.assertEqual(normalize_phone_number("+1-555-123-4567"), "15551234567")

    def test_phone_with_extension(self):
        """Phone numbers with extensions should preserve x."""
        result = normalize_phone_number("555-123-4567x123")
        self.assertIn("x", result.lower())

    def test_short_test_number(self):
        """Short test numbers (like 42) should be handled."""
        result = normalize_phone_number("42")
        self.assertEqual(result, "42")


class TestGenericValidatePhoneNumber(TestCase):
    """Test phone number validation."""

    def test_valid_phone_number(self):
        """Valid phone numbers should pass."""
        self.assertEqual(generic_validate_phone_number("5551234567"), "5551234567")
        self.assertEqual(generic_validate_phone_number("555-123-4567"), "5551234567")

    def test_phone_with_country_code(self):
        """Phone with leading 1 and > 11 digits should strip the 1."""
        result = generic_validate_phone_number("15551234567890")
        self.assertEqual(result[0], "5")  # Leading 1 stripped

    def test_invalid_short_phone(self):
        """Too short phone numbers should fail (except 42)."""
        with self.assertRaises(ValidationError):
            generic_validate_phone_number("12345")

    def test_special_test_number(self):
        """The special test number 42 should be allowed."""
        self.assertEqual(generic_validate_phone_number("42"), "42")


class TestSanitizeUrlForLogging(TestCase):
    """Test URL sanitization for safe logging."""

    def test_normal_url(self):
        """Normal URLs should pass through."""
        url = "https://example.com/path"
        self.assertEqual(_sanitize_url_for_logging(url), url)

    def test_long_url_truncated(self):
        """Long URLs should be truncated."""
        long_url = "https://example.com/" + "a" * 200
        result = _sanitize_url_for_logging(long_url)
        self.assertEqual(len(result), 103)  # 100 + "..."
        self.assertTrue(result.endswith("..."))

    def test_newlines_removed(self):
        """Newlines should be removed to prevent log injection."""
        url = "https://example.com\nmalicious\rlog"
        result = _sanitize_url_for_logging(url)
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)


class TestValidateRedirectUrl(TestCase):
    """Test redirect URL validation for open redirect prevention."""

    def test_valid_production_url(self):
        """Valid production URLs should pass."""
        url = "https://fighthealthinsurance.com/dashboard"
        result = validate_redirect_url(url, "https://default.com")
        self.assertEqual(result, url)

    def test_valid_fightpaperwork_url(self):
        """Valid fightpaperwork URLs should pass."""
        url = "https://www.fightpaperwork.com/settings"
        result = validate_redirect_url(url, "https://default.com")
        self.assertEqual(result, url)

    def test_localhost_http_allowed(self):
        """HTTP should be allowed for localhost."""
        url = "http://localhost:8000/callback"
        result = validate_redirect_url(url, "https://default.com")
        self.assertEqual(result, url)

    def test_invalid_domain_rejected(self):
        """URLs from unknown domains should return default."""
        url = "https://malicious-site.com/phishing"
        default = "https://fighthealthinsurance.com/"
        result = validate_redirect_url(url, default)
        self.assertEqual(result, default)

    def test_http_non_localhost_rejected(self):
        """HTTP should be rejected for non-localhost domains."""
        url = "http://fighthealthinsurance.com/page"
        default = "https://fighthealthinsurance.com/"
        result = validate_redirect_url(url, default)
        self.assertEqual(result, default)

    def test_invalid_scheme_rejected(self):
        """Non-http(s) schemes should be rejected."""
        url = "javascript:alert('xss')"
        default = "https://fighthealthinsurance.com/"
        result = validate_redirect_url(url, default)
        self.assertEqual(result, default)

    def test_none_url_returns_default(self):
        """None URL should return default."""
        default = "https://fighthealthinsurance.com/"
        result = validate_redirect_url(None, default)
        self.assertEqual(result, default)

    def test_empty_url_returns_default(self):
        """Empty URL should return default."""
        default = "https://fighthealthinsurance.com/"
        result = validate_redirect_url("", default)
        self.assertEqual(result, default)


class TestGetAllowedRedirectDomains(TestCase):
    """Test allowed redirect domains configuration."""

    def test_default_domains_included(self):
        """Default production domains should be included."""
        domains = _get_allowed_redirect_domains()
        self.assertIn("fighthealthinsurance.com", domains)
        self.assertIn("fightpaperwork.com", domains)
        self.assertIn("localhost", domains)

    @override_settings(ALLOWED_REDIRECT_DOMAINS=["custom.example.com"])
    def test_custom_domains_from_settings(self):
        """Custom domains from settings should override defaults."""
        domains = _get_allowed_redirect_domains()
        self.assertIn("custom.example.com", domains)
        self.assertNotIn("fighthealthinsurance.com", domains)


class TestGetDomainIdFromRequest(TestCase):
    """Test resolving the domain id from a request session or username."""

    def _build_request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        request.session = SessionStore()
        return request

    def _make_user(self, username):
        return get_user_model().objects.create_user(
            username=username,
            password="SecureP@ss123",
        )

    def test_recovers_domain_from_username_when_session_missing(self):
        """When the session lacks domain_id, derive it from the username."""
        domain_id = "550e8400-e29b-41d4-a716-446655440000"
        user = self._make_user(f"patient@example.com🐼{domain_id}")
        request = self._build_request(user)
        self.assertNotIn("domain_id", request.session)

        result = get_domain_id_from_request(request)

        self.assertEqual(result, domain_id)
        # The recovered value is cached back onto the session.
        self.assertEqual(request.session["domain_id"], domain_id)

    def test_prefers_session_domain_id_when_present(self):
        """A truthy session domain_id takes precedence over the username."""
        user = self._make_user("patient@example.com🐼username-domain")
        request = self._build_request(user)
        request.session["domain_id"] = "session-domain"

        result = get_domain_id_from_request(request)

        self.assertEqual(result, "session-domain")

    def test_unauthenticated_user_raises(self):
        """An unauthenticated user with no session domain_id raises."""
        from django.contrib.auth.models import AnonymousUser

        request = self._build_request(AnonymousUser())
        self.assertFalse(request.user.is_authenticated)

        with self.assertRaises(Exception):
            get_domain_id_from_request(request)

    def test_missing_user_attribute_raises(self):
        """A request without a .user attribute raises."""
        request = RequestFactory().get("/")
        request.session = SessionStore()
        self.assertFalse(hasattr(request, "user"))

        with self.assertRaises(Exception):
            get_domain_id_from_request(request)

"""
Tests for the simplified audit logging system.
"""

from typing import Optional
from unittest.mock import patch

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
    tracking_metadata_for_request,
    TrackingInfo,
)
from fhi_users.models import ProfessionalUser

User = get_user_model()


# Mock model for testing TrackingInfo methods
class MockModelWithTrackingFields:
    """Mock model with tracking fields for testing."""

    user_agent: str
    asn: str
    asn_name: str
    ip_address: Optional[str]

    def __init__(self):
        self.user_agent = ""
        self.asn = ""
        self.asn_name = ""
        self.ip_address = None


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

    def test_user_agent_stored_for_non_professional(self):
        """Test that user_agent is always stored, even for non-professional users."""
        request = self.factory.get(
            "/", HTTP_USER_AGENT="TestBrowser/1.0", HTTP_X_FORWARDED_FOR="1.2.3.4"
        )
        request.user = self.user

        log = log_event(EventType.LOGIN_SUCCESS, request=request, user=self.user)

        self.assertIsNotNone(log)
        # User agent should be stored for all users
        self.assertEqual(log.user_agent, "TestBrowser/1.0")
        # IP should NOT be stored for non-professional users
        self.assertIsNone(log.ip_address)

    def test_user_agent_and_ip_stored_for_professional(self):
        """Test that both user_agent and IP are stored for professional users."""
        # Create a professional user
        pro_user = User.objects.create_user(
            username="prouser",
            email="pro@example.com",
            password="testpass123",
        )
        ProfessionalUser.objects.create(
            user=pro_user,
            active=True,
        )

        request = self.factory.get(
            "/", HTTP_USER_AGENT="TestBrowser/2.0", HTTP_X_FORWARDED_FOR="5.6.7.8"
        )
        request.user = pro_user

        log = log_event(EventType.LOGIN_SUCCESS, request=request, user=pro_user)

        self.assertIsNotNone(log)
        # User agent should be stored
        self.assertEqual(log.user_agent, "TestBrowser/2.0")
        # IP should be stored for professional users
        self.assertEqual(log.ip_address, "5.6.7.8")


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

    def test_tracking_metadata_bounds_overlong_ip(self):
        """An over-long X-Forwarded-For is truncated before reaching Stripe.

        Stripe rejects metadata values over 500 chars and the value is later
        persisted into a bounded column, so a garbage/spoofed header must be
        truncated rather than break checkout creation or the expiry webhook.
        """
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="9" * 600)
        md = tracking_metadata_for_request(request)
        self.assertEqual(md["ip_address"], "9" * 64)

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
        info = TrackingInfo(
            user_agent="TestAgent/2.0",
            ip_address="10.0.0.1",
            asn="AS9999",
            asn_name="Test ASN",
        )
        model = MockModelWithTrackingFields()
        info.update_model_fields(model)

        self.assertEqual(model.user_agent, "TestAgent/2.0")
        self.assertEqual(model.ip_address, "10.0.0.1")
        self.assertEqual(model.asn, "AS9999")
        self.assertEqual(model.asn_name, "Test ASN")

    def test_to_model_kwargs_bounds_overlong_ip(self):
        """An over-long header IP is truncated before it reaches a model field."""
        info = TrackingInfo(ip_address="9" * 200)
        self.assertEqual(info.to_model_kwargs()["ip_address"], "9" * 64)

    def test_update_model_fields_bounds_overlong_ip(self):
        """update_model_fields truncates an over-long header IP to column width."""
        info = TrackingInfo(ip_address="9" * 200)
        model = MockModelWithTrackingFields()
        info.update_model_fields(model)
        self.assertEqual(model.ip_address, "9" * 64)

    def test_tracking_ip_address_fields_are_char(self):
        """Columns that store raw header IPs are free-form text, not a validating
        GenericIPAddressField (which would raise on a spoofed/malformed value)."""
        from django.db import models as dj_models
        from fighthealthinsurance.models import DemoRequests, Denial, OngoingChat

        for model in (DemoRequests, Denial, OngoingChat):
            field = model._meta.get_field("ip_address")
            self.assertIsInstance(
                field, dj_models.CharField, f"{model.__name__}.ip_address"
            )


class DenialTrackingInfoTest(TestCase):
    """Tests for tracking info storage in Denial model."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_denial_stores_tracking_info(self):
        """Test that a Denial stores ASN and user agent tracking info."""
        from fighthealthinsurance.models import Denial

        tracking_info = TrackingInfo(
            user_agent="TestBrowser/1.0",
            ip_address=None,  # Not stored for non-professionals
            asn="AS12345",
            asn_name="Test ISP",
        )

        denial = Denial.objects.create(
            denial_text="Test denial text",
            **tracking_info.to_model_kwargs(),
        )

        # Verify tracking info was stored
        self.assertEqual(denial.user_agent, "TestBrowser/1.0")
        self.assertEqual(denial.asn, "AS12345")
        self.assertEqual(denial.asn_name, "Test ISP")
        self.assertIsNone(denial.ip_address)

    def test_denial_stores_ip_for_professional(self):
        """Test that IP is stored for professional users."""
        from fighthealthinsurance.models import Denial

        tracking_info = TrackingInfo(
            user_agent="ProfessionalBrowser/2.0",
            ip_address="192.168.1.100",  # Stored for professionals
            asn="AS54321",
            asn_name="Professional ISP",
        )

        denial = Denial.objects.create(
            denial_text="Professional denial text",
            **tracking_info.to_model_kwargs(),
        )

        # Verify tracking info including IP was stored
        self.assertEqual(denial.user_agent, "ProfessionalBrowser/2.0")
        self.assertEqual(denial.asn, "AS54321")
        self.assertEqual(denial.asn_name, "Professional ISP")
        self.assertEqual(denial.ip_address, "192.168.1.100")

    def test_denial_tracking_from_request(self):
        """Test extracting tracking info from request and storing in Denial."""
        from fighthealthinsurance.models import Denial

        factory = RequestFactory()
        request = factory.post(
            "/test/",
            HTTP_USER_AGENT="RequestBrowser/3.0",
            HTTP_X_FORWARDED_FOR="10.0.0.1",
        )

        # Extract tracking info for non-professional
        tracking_info = extract_tracking_info(request, is_professional=False)

        denial = Denial.objects.create(
            denial_text="Request denial text",
            **tracking_info.to_model_kwargs(),
        )

        # Verify tracking info from request
        self.assertEqual(denial.user_agent, "RequestBrowser/3.0")
        # IP should not be stored for non-professional
        self.assertIsNone(denial.ip_address)


class GuessUsStateTest(TestCase):
    """Tests for the IP -> US state guess used to seed chat context.

    Lookups are gated on FHI_GEOIP_CITY_DB (state data needs a city-level
    database); these tests inject a fake geoip2fast module into sys.modules
    and reset the cached reader around each case.
    """

    FAKE_DB = {"FHI_GEOIP_CITY_DB": "/fake/geoip2fast-city.dat.gz"}

    def setUp(self):
        import fhi_users.audit as audit_module

        self.audit = audit_module
        self.audit._reset_geo_reader_cache_for_tests()

    def tearDown(self):
        import sys

        sys.modules.pop("geoip2fast", None)
        self.audit._reset_geo_reader_cache_for_tests()

    def _install_fake_geoip(self, lookup_result):
        """Install a fake geoip2fast module whose lookup returns the given
        object (or raises it, if it's an exception instance)."""
        import sys
        import types

        fake_module = types.ModuleType("geoip2fast")

        class FakeGeoIP2Fast:
            def __init__(self, geoip2fast_data_file=None):
                self.data_file = geoip2fast_data_file

            def lookup(self, ip):
                if isinstance(lookup_result, Exception):
                    raise lookup_result
                return lookup_result

        fake_module.GeoIP2Fast = FakeGeoIP2Fast  # type: ignore[attr-defined]
        sys.modules["geoip2fast"] = fake_module

    def test_none_ip_returns_none(self):
        self.assertIsNone(self.audit.guess_us_state(None))
        self.assertIsNone(self.audit.guess_us_state(""))

    def test_no_db_key_returns_none(self):
        # The key is the switch: without FHI_GEOIP_CITY_DB no reader is
        # built, even with geoip2fast importable.
        class Result:
            country_code = "US"
            subdivision_code = "CA"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FHI_GEOIP_CITY_DB", None)
            self.assertIsNone(self.audit.guess_us_state("203.0.113.7"))

    def test_us_subdivision_code_maps_to_state_name(self):
        class Result:
            country_code = "US"
            subdivision_code = "CA"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertEqual(self.audit.guess_us_state("203.0.113.7"), "California")

    def test_country_prefixed_subdivision_code(self):
        class Result:
            country_code = "US"
            subdivision_code = "US-NY"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertEqual(self.audit.guess_us_state("203.0.113.7"), "New York")

    def test_subdivision_name_on_nested_city_object(self):
        # Matches the real geoip2fast CityDetail shape (city.subdivision_*).
        class City:
            subdivision_name = "Texas"

        class Result:
            country_code = "US"
            city = City()

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertEqual(self.audit.guess_us_state("203.0.113.7"), "Texas")

    def test_empty_country_returns_none(self):
        # A subdivision without an explicit US country code is unconfirmed
        # data, not a US state.
        class Result:
            country_code = ""
            subdivision_code = "CA"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertIsNone(self.audit.guess_us_state("203.0.113.7"))

    def test_non_us_ip_returns_none(self):
        class Result:
            country_code = "FR"
            subdivision_code = "IDF"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertIsNone(self.audit.guess_us_state("203.0.113.7"))

    def test_country_only_data_returns_none(self):
        class Result:
            country_code = "US"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertIsNone(self.audit.guess_us_state("203.0.113.7"))

    def test_lookup_error_returns_none(self):
        self._install_fake_geoip(RuntimeError("boom"))
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertIsNone(self.audit.guess_us_state("203.0.113.7"))

    def test_unknown_subdivision_returns_none(self):
        class Result:
            country_code = "US"
            subdivision_code = "ZZ"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertIsNone(self.audit.guess_us_state("203.0.113.7"))


class GetAsnInfoTest(TestCase):
    """get_asn_info shares the gated cached reader with guess_us_state."""

    FAKE_DB = {"FHI_GEOIP_CITY_DB": "/fake/geoip2fast-city-asn.dat.gz"}

    def setUp(self):
        import fhi_users.audit as audit_module

        self.audit = audit_module
        self.audit._reset_geo_reader_cache_for_tests()

    def tearDown(self):
        import sys

        sys.modules.pop("geoip2fast", None)
        self.audit._reset_geo_reader_cache_for_tests()

    def _install_fake_geoip(self, lookup_result):
        import sys
        import types

        fake_module = types.ModuleType("geoip2fast")

        class FakeGeoIP2Fast:
            def __init__(self, geoip2fast_data_file=None):
                self.data_file = geoip2fast_data_file

            def lookup(self, ip):
                return lookup_result

        fake_module.GeoIP2Fast = FakeGeoIP2Fast  # type: ignore[attr-defined]
        sys.modules["geoip2fast"] = fake_module

    def test_no_db_key_returns_empty(self):
        import os

        # patch.dict snapshots os.environ and restores it on exit, so the
        # pop below cannot leak into later tests.
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("FHI_GEOIP_CITY_DB", None)
            self.audit._reset_geo_reader_cache_for_tests()
            self.assertEqual(self.audit.get_asn_info("203.0.113.7"), ("", ""))

    def test_asn_name_from_reader(self):
        class Result:
            asn_name = "EXAMPLE-NET"

        self._install_fake_geoip(Result())
        with patch.dict("os.environ", self.FAKE_DB):
            self.assertEqual(
                self.audit.get_asn_info("203.0.113.7"), ("", "EXAMPLE-NET")
            )

    def test_none_ip_returns_empty(self):
        self.assertEqual(self.audit.get_asn_info(None), ("", ""))


class GeoLookupStatusTest(TestCase):
    """The startup soft-fail status/warning helper."""

    def setUp(self):
        import fhi_users.audit as audit_module

        self.audit = audit_module

    def test_disabled_without_key_names_the_key(self):
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("FHI_GEOIP_CITY_DB", None)
            enabled, reason = self.audit.geo_lookup_status()
        self.assertFalse(enabled)
        self.assertIn("FHI_GEOIP_CITY_DB", reason)

    def test_disabled_when_file_missing(self):
        with patch.dict("os.environ", {"FHI_GEOIP_CITY_DB": "/nonexistent/geo.dat.gz"}):
            enabled, reason = self.audit.geo_lookup_status()
        self.assertFalse(enabled)
        self.assertIn("does not exist", reason)

    def test_enabled_with_real_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".dat.gz") as f:
            f.write(b"not-really-a-db-but-nonempty")
            f.flush()
            with patch.dict("os.environ", {"FHI_GEOIP_CITY_DB": f.name}):
                enabled, reason = self.audit.geo_lookup_status()
        # geoip2fast is an installed dependency, so a present, readable,
        # non-empty file => enabled (deep validation happens lazily at first
        # lookup, with its own warning).
        self.assertTrue(enabled)
        self.assertIsNone(reason)

    def test_disabled_when_file_empty(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".dat.gz") as f:
            with patch.dict("os.environ", {"FHI_GEOIP_CITY_DB": f.name}):
                enabled, reason = self.audit.geo_lookup_status()
        self.assertFalse(enabled)
        self.assertIn("empty", reason)

    def test_warn_helper_never_raises(self):
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("FHI_GEOIP_CITY_DB", None)
            # Reset the once-per-process guard so the warning path runs.
            self.audit._geo_startup_warning_emitted = False
            self.audit.warn_if_geo_lookups_disabled()
            self.audit.warn_if_geo_lookups_disabled()  # second call: no-op

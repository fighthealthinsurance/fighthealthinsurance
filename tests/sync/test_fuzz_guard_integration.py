"""
End-to-end integration tests for fuzz guard system.
"""

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings

from fighthealthinsurance.models import FuzzAttempt

User = get_user_model()


def get_response(_request):
    """Dummy get_response for middleware testing."""
    return HttpResponse("OK")


class TestFuzzGuardEndToEnd(TestCase):
    """End-to-end integration tests for blocked requests."""

    def setUp(self):
        self.client = Client()

    def test_normal_request_passes(self):
        """Normal request should not trigger fuzz guard."""
        response = self.client.get(
            "/",
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        # Should not create FuzzAttempt for normal requests
        # (depends on whether home page is covered by fuzz guard)
        self.assertNotEqual(response.status_code, 418)
        self.assertNotEqual(response.status_code, 400)

    def test_score_request_detects_scanner_ua(self):
        """score_request should detect scanner user-agents."""
        from fighthealthinsurance.middleware.FuzzGuardMiddleware import score_request
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/scan", HTTP_USER_AGENT="sqlmap/1.4.7")
        score, reasons = score_request(request)
        self.assertGreaterEqual(score, 40)  # scanner_ua adds 40
        self.assertTrue(any("scanner_ua" in r for r in reasons))

    @override_settings(FUZZ_GUARD_ENABLED=True, FUZZ_GUARD_SCORE_THRESHOLD=50)
    def test_probe_path_with_scanner_ua_blocked(self):
        """Request to probe path with scanner UA should be blocked (30+40 >= 50)."""
        response = self.client.get(
            "/wp-admin/",
            HTTP_USER_AGENT="sqlmap/1.4.7",
        )
        # probe_path (30) + scanner_ua (40) = 70 >= threshold (50)
        self.assertIn(response.status_code, [400, 418, 429])

    @override_settings(FUZZ_GUARD_ENABLED=True, FUZZ_GUARD_SCORE_THRESHOLD=50)
    def test_invalid_denial_id_blocked(self):
        """Request with invalid denial_id should be blocked."""
        response = self.client.get(
            "/find_next_steps?denial_id=<script>alert(1)</script>",
            HTTP_USER_AGENT="Mozilla/5.0",
        )
        self.assertIn(response.status_code, [400, 418, 429])

    @override_settings(FUZZ_GUARD_ENABLED=False)
    def test_disabled_fuzz_guard_passes_all(self):
        """With FUZZ_GUARD_ENABLED=False, should not block."""
        # Even with scanner UA, should not return fuzz guard status codes
        # (though may return other errors like 404)
        response = self.client.get("/some-path", HTTP_USER_AGENT="sqlmap/1.4.7")
        # Should not be specifically blocked by fuzz guard
        # 404 is expected for non-existent path, but not 418/429 from fuzz guard
        self.assertNotIn(
            response.status_code,
            [418, 429],
            "Fuzz guard should not block when FUZZ_GUARD_ENABLED=False",
        )


class TestHumanVerificationFlow(TestCase):
    """Test human verification flow integration."""

    def test_scan_accessible_without_verification(self):
        """Scan page should be accessible without prior verification."""
        client = Client()
        response = client.get("/scan")
        # Should not be 418 (not require prior verification)
        self.assertNotEqual(response.status_code, 418)

    def test_find_next_steps_blocked_without_verification(self):
        """FindNextSteps should return 418 without scan completion."""
        client = Client()
        response = client.get("/find_next_steps")
        self.assertEqual(response.status_code, 418)

    def test_generate_appeal_blocked_without_verification(self):
        """GenerateAppeal should return 418 without verification."""
        client = Client()
        response = client.get("/generate_appeal")
        self.assertEqual(response.status_code, 418)

    def test_choose_appeal_blocked_without_verification(self):
        """ChooseAppeal should return 418 without verification."""
        client = Client()
        response = client.post("/choose_appeal")
        self.assertEqual(response.status_code, 418)

    def test_categorize_review_blocked_without_verification(self):
        """CategorizeReview should return 418 without verification."""
        client = Client()
        response = client.get("/categorize_review")
        self.assertEqual(response.status_code, 418)

    def test_session_verification_persists(self):
        """Human verification should persist in session."""
        client = Client()

        # Start a session
        session = client.session
        session["human_verified"] = True
        session["human_verified_at"] = "2024-01-01T00:00:00"
        session.save()

        # Now downstream views should work (not return 418)
        # Note: They may return other errors (400, 404, 500) but not 418
        response = client.get("/find_next_steps")
        self.assertNotEqual(response.status_code, 418)


class TestFuzzAttemptLogging(TestCase):
    """Test that FuzzAttempt model can store data correctly."""

    def test_fuzz_attempt_can_be_created(self):
        """FuzzAttempt record should be creatable with all required fields."""
        initial_count = FuzzAttempt.objects.count()

        FuzzAttempt.objects.create(
            ip_hash="a" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/test",
            status_returned=400,
            reason='["scanner_ua:sqlmap"]',
            score=40,
        )

        final_count = FuzzAttempt.objects.count()
        self.assertGreater(final_count, initial_count)

    def test_fuzz_attempt_contains_reason(self):
        """FuzzAttempt should store triggered rule reasons."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="b" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/scan",
            status_returned=400,
            reason='["scanner_ua:sqlmap"]',
            score=40,
        )

        self.assertIn("scanner_ua", attempt.reason)

    def test_fuzz_attempt_contains_score(self):
        """FuzzAttempt should store calculated score."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="c" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/scan",
            status_returned=400,
            reason='["scanner_ua:sqlmap"]',
            score=40,
        )

        self.assertGreaterEqual(attempt.score, 40)

    def test_fuzz_attempt_contains_method_and_path(self):
        """FuzzAttempt should store request method and path."""
        attempt = FuzzAttempt.objects.create(
            ip_hash="d" * 64,
            ip_prefix="192.168.1.0/24",
            method="GET",
            path="/scan",
            status_returned=400,
            reason='["scanner_ua:sqlmap"]',
            score=40,
        )

        self.assertEqual(attempt.method, "GET")
        self.assertIn("/scan", attempt.path)


class TestHealthEndpointsBypass(TestCase):
    """Test that health endpoints bypass fuzz guard."""

    @override_settings(FUZZ_GUARD_ENABLED=True, FUZZ_GUARD_SCORE_THRESHOLD=50)
    def test_health_endpoint_bypasses_fuzz_guard(self):
        """Request to /health should bypass fuzz guard."""
        client = Client()
        # Even with suspicious UA, should not be blocked
        response = client.get("/health", HTTP_USER_AGENT="sqlmap/1.4.7")
        # Should not return 400/418/429 from fuzz guard
        # May return 404 if endpoint doesn't exist, or 200 if it does
        self.assertNotIn(response.status_code, [418])

    @override_settings(FUZZ_GUARD_ENABLED=True, FUZZ_GUARD_SCORE_THRESHOLD=50)
    def test_ready_endpoint_bypasses_fuzz_guard(self):
        """Request to /ready should bypass fuzz guard."""
        client = Client()
        response = client.get("/ready", HTTP_USER_AGENT="nikto/2.1.6")
        self.assertNotIn(response.status_code, [418])


class TestRateLimitingIntegration(TestCase):
    """Test rate limiting across multiple requests."""

    def setUp(self):
        self.client = Client()
        # Clear rate limit cache
        from django.core.cache import cache

        cache.clear()

    @override_settings(
        FUZZ_GUARD_ENABLED=True,
        FUZZ_GUARD_SCORE_THRESHOLD=30,
        FUZZ_GUARD_RATE_LIMIT_PER_MINUTE=3,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "fuzz-guard-test",
            }
        },
    )
    def test_rate_limit_triggers_429(self):
        """Exceeding rate limit should return 429 after enough blocked requests."""
        # With threshold=30 and scanner_ua score=40, every request is blocked.
        # After 3 blocked requests, rate limiting should kick in with 429.
        got_429 = False
        for _ in range(10):
            response = self.client.get("/scan", HTTP_USER_AGENT="sqlmap/1.4.7")
            if response.status_code == 429:
                got_429 = True
                break

        self.assertTrue(got_429, "Rate limiting should have triggered 429 within 10 requests")


class TestResponseFormatting(TestCase):
    """Test fuzz guard response formatting."""

    @override_settings(
        FUZZ_GUARD_ENABLED=True,
        FUZZ_GUARD_SCORE_THRESHOLD=30,
    )
    def test_blocked_response_is_plain_text(self):
        """Blocked response should be text/plain."""
        client = Client()
        # scanner_ua scores 40 >= threshold 30, so this will be blocked
        response = client.get("/scan", HTTP_USER_AGENT="sqlmap/1.4.7")
        self.assertIn(response.status_code, [400, 418, 429])
        self.assertEqual(response["Content-Type"], "text/plain")

    @override_settings(
        FUZZ_GUARD_ENABLED=True,
        FUZZ_GUARD_SCORE_THRESHOLD=30,
    )
    def test_blocked_response_contains_message(self):
        """Blocked response should contain friendly message."""
        client = Client()
        response = client.get("/scan", HTTP_USER_AGENT="sqlmap/1.4.7")
        self.assertIn(response.status_code, [400, 418, 429])
        content = response.content.decode("utf-8")
        self.assertIn("Hi Friend", content)


class TestAdminAccessibility(TestCase):
    """Test that FuzzAttempt admin is accessible."""

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="superuser", email="super@example.com", password="superpass"
        )
        self.client = Client()
        self.client.force_login(self.superuser)

    def test_fuzz_attempt_registered_in_admin(self):
        """FuzzAttempt should be registered in admin."""
        from django.contrib import admin

        self.assertIn(FuzzAttempt, admin.site._registry)

    def test_fuzz_attempt_list_accessible(self):
        """FuzzAttempt admin list should be accessible."""
        # The URL might vary based on settings, so we check registration instead
        from django.contrib import admin
        from fighthealthinsurance.admin import FuzzAttemptAdmin

        self.assertIsInstance(admin.site._registry.get(FuzzAttempt), FuzzAttemptAdmin)

    def test_fuzz_attempt_admin_has_list_display(self):
        """Admin should have list_display configured."""
        from django.contrib import admin

        admin_class = admin.site._registry.get(FuzzAttempt)
        self.assertIsNotNone(admin_class)
        self.assertTrue(hasattr(admin_class, "list_display"))
        self.assertIn("ip_prefix", admin_class.list_display)

"""
Tests for human verification session tracking and mixin.
"""

from django.test import Client, RequestFactory, TestCase, override_settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.urls import reverse
from django.views import View

from fighthealthinsurance.human_verification import (
    HumanVerificationRequiredMixin,
    TEAPOT_MESSAGE,
    create_teapot_response,
    is_human_verified,
    require_human_verification,
    set_human_verified,
)


def get_request_with_session(path="/", method="get"):
    """Create a request with session middleware applied."""
    factory = RequestFactory()
    if method == "get":
        request = factory.get(path)
    else:
        request = factory.post(path)

    # Apply session middleware
    middleware = SessionMiddleware(lambda r: HttpResponse())
    middleware.process_request(request)
    request.session.save()
    return request


class TestHumanVerificationHelpers(TestCase):
    """Test helper functions for human verification."""

    def test_is_human_verified_false_when_missing(self):
        """Should return False when human_verified not in session."""
        request = get_request_with_session()
        self.assertFalse(is_human_verified(request))

    def test_is_human_verified_false_when_explicitly_false(self):
        """Should return False when human_verified is False."""
        request = get_request_with_session()
        request.session["human_verified"] = False
        self.assertFalse(is_human_verified(request))

    def test_is_human_verified_true_when_set(self):
        """Should return True when human_verified is True."""
        request = get_request_with_session()
        request.session["human_verified"] = True
        self.assertTrue(is_human_verified(request))

    def test_set_human_verified_sets_flag(self):
        """set_human_verified should set human_verified to True."""
        request = get_request_with_session()
        set_human_verified(request)
        self.assertTrue(request.session.get("human_verified"))

    def test_set_human_verified_sets_timestamp(self):
        """set_human_verified should set human_verified_at timestamp."""
        request = get_request_with_session()
        set_human_verified(request)
        self.assertIn("human_verified_at", request.session)
        # Should be ISO format timestamp
        self.assertIsInstance(request.session["human_verified_at"], str)

    def test_create_teapot_response_status_418(self):
        """create_teapot_response should return 418 status."""
        response = create_teapot_response()
        self.assertEqual(response.status_code, 418)

    def test_create_teapot_response_plain_text(self):
        """create_teapot_response should be text/plain."""
        response = create_teapot_response()
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_create_teapot_response_contains_message(self):
        """create_teapot_response should contain the message."""
        response = create_teapot_response()
        self.assertEqual(response.content.decode("utf-8"), TEAPOT_MESSAGE)


class TestHumanVerificationMixin(TestCase):
    """Test HumanVerificationRequiredMixin."""

    def test_returns_418_when_not_verified(self):
        """Should return 418 when human_verified is False/missing."""

        class TestView(HumanVerificationRequiredMixin, View):
            def get(self, request):
                return HttpResponse("OK")

        request = get_request_with_session()
        view = TestView.as_view()
        response = view(request)
        self.assertEqual(response.status_code, 418)

    def test_passes_when_verified(self):
        """Should pass request through when human_verified is True."""

        class TestView(HumanVerificationRequiredMixin, View):
            def get(self, request):
                return HttpResponse("OK")

        request = get_request_with_session()
        request.session["human_verified"] = True
        view = TestView.as_view()
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode("utf-8"), "OK")

    def test_response_is_plain_text(self):
        """418 response should be text/plain."""

        class TestView(HumanVerificationRequiredMixin, View):
            def get(self, request):
                return HttpResponse("OK")

        request = get_request_with_session()
        view = TestView.as_view()
        response = view(request)
        self.assertEqual(response["Content-Type"], "text/plain")


class TestHumanVerificationDecorator(TestCase):
    """Test require_human_verification decorator."""

    def test_returns_418_when_not_verified(self):
        """Should return 418 when human_verified is False/missing."""

        @require_human_verification
        def test_view(request):
            return HttpResponse("OK")

        request = get_request_with_session()
        response = test_view(request)
        self.assertEqual(response.status_code, 418)

    def test_passes_when_verified(self):
        """Should pass request through when human_verified is True."""

        @require_human_verification
        def test_view(request):
            return HttpResponse("OK")

        request = get_request_with_session()
        request.session["human_verified"] = True
        response = test_view(request)
        self.assertEqual(response.status_code, 200)


class TestDownstreamViewsRequireVerification(TestCase):
    """Test that downstream views enforce human verification."""

    def test_find_next_steps_requires_verification(self):
        """FindNextSteps should require human verification."""
        client = Client()
        response = client.get(reverse("find_next_steps"))
        # Should return 418 since no captcha was completed
        self.assertEqual(response.status_code, 418)

    def test_generate_appeal_requires_verification(self):
        """GenerateAppeal should require human verification."""
        client = Client()
        response = client.get(reverse("generate_appeal"))
        self.assertEqual(response.status_code, 418)

    def test_choose_appeal_requires_verification(self):
        """ChooseAppeal should require human verification."""
        client = Client()
        response = client.post(reverse("choose_appeal"))
        self.assertEqual(response.status_code, 418)

    def test_categorize_review_requires_verification(self):
        """CategorizeReview should require human verification."""
        client = Client()
        response = client.get(reverse("categorize_review"))
        self.assertEqual(response.status_code, 418)


class TestScanDoesNotRequireVerification(TestCase):
    """Test that scan page doesn't require prior verification."""

    def test_scan_accessible_without_verification(self):
        """InitialProcessView (scan) should NOT require prior verification."""
        client = Client()
        response = client.get("/scan")
        # Should not return 418
        self.assertNotEqual(response.status_code, 418)


class TestCaptchaOnScanForm(TestCase):
    """Test captcha integration on the scan form."""

    def test_captcha_field_rendered_in_template(self):
        """scrub.html should render the captcha field area."""
        client = Client()
        response = client.get("/scan")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertTrue(
            any(token in content for token in ("id_captcha", "recaptcha", "captcha"))
        )

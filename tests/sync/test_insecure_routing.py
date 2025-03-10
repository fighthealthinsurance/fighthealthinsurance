from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse

from fighthealthinsurance.views import SessionRequiredMixin

from fighthealthinsurance import models


class MockView(SessionRequiredMixin):
    """A simple view for testing the SessionRequiredMixin."""

    def get(self, request, *args, **kwargs):
        return HttpResponse("Success")

    def post(self, request, *args, **kwargs):
        return HttpResponse("Success")


class SessionRequiredMixinTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client()

        self.denial = models.Denial.objects.create(
            denial_id=12345,
            hashed_email="test-hashed-email",
            semi_sekret="test-semi-sekret",
        )

    def _add_session_to_request(self, request):
        """Helper method to add session to a request."""
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()

    def test_redirect_without_session(self):
        """Test that the mixin redirects to 'process' when no session exists."""
        request = self.factory.get("/test-url/")
        self._add_session_to_request(request)

        view = MockView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("process"))

    def test_proceed_with_session(self):
        """Test that the mixin allows the request when session exists."""
        request = self.factory.get("/test-url/")
        self._add_session_to_request(request)
        request.session["denial_id"] = 12345

        view = MockView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Success")

    def test_proceed_with_denial_id_in_get(self):
        """Test that the mixin sets session when denial_id is in GET params."""
        request = self.factory.get("/test-url/?denial_id=12345")
        self._add_session_to_request(request)

        view = MockView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(request.session["denial_id"]), 12345)

    def test_proceed_with_denial_id_in_post(self):
        """Test that the mixin sets session when denial_id is in POST data."""
        request = self.factory.post("/test-url/", {"denial_id": 12345})
        self._add_session_to_request(request)

        view = MockView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(request.session["denial_id"]), 12345)


class EntityExtractViewTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.denial = models.Denial.objects.create(
            denial_id=12345,
            hashed_email="test-hashed-email",
            semi_sekret="test-semi-sekret",
        )

    def test_view_requires_session(self):
        """Test that the view requires a session."""
        response = self.client.get(reverse("eev"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("process"))

    def test_view_with_session(self):
        """Test that the view works with a session."""
        session = self.client.session
        session["denial_id"] = 12345
        session.save()

        response = self.client.get(reverse("eev"))
        self.assertNotEqual(response.status_code, 302)
        self.assertNotEqual(
            response.url if hasattr(response, "url") else None, reverse("process")
        )


class PlanDocumentsViewTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.denial = models.Denial.objects.create(
            denial_id=12345,
            hashed_email="test-hashed-email",
            semi_sekret="test-semi-sekret",
        )

    def test_view_requires_session(self):
        """Test that the view requires a session."""
        response = self.client.get(reverse("hh"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("process"))

    def test_view_with_session(self):
        """Test that the view works with a session."""
        session = self.client.session
        session["denial_id"] = 12345
        session.save()

        response = self.client.get(reverse("hh"))
        self.assertNotEqual(response.status_code, 302)
        self.assertNotEqual(
            response.url if hasattr(response, "url") else None, reverse("process")
        )


class DenialCollectedViewTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.denial = models.Denial.objects.create(
            denial_id=12345,
            hashed_email="test-hashed-email",
            semi_sekret="test-semi-sekret",
        )

    def test_view_requires_session(self):
        """Test that the view requires a session."""
        response = self.client.get(reverse("dvc"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("process"))

    def test_view_with_session(self):
        """Test that the view works with a session."""

        session = self.client.session
        session["denial_id"] = 12345
        session.save()

        response = self.client.get(reverse("dvc"))
        self.assertNotEqual(response.status_code, 302)
        self.assertNotEqual(
            response.url if hasattr(response, "url") else None, reverse("process")
        )

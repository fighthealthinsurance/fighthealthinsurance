"""Regression tests: POSTing invalid input to appeal-flow handlers must not 500.

Previously several POST handlers returned ``None`` on an invalid form (which
makes Django raise "didn't return an HttpResponse" -> HTTP 500), and
ShareAppealView raised ``Denial.DoesNotExist`` -> 500 for a wrong
denial_id/email combo. These handlers should redirect (or otherwise return a
non-5xx response) instead.
"""

from django.test import TestCase, Client, RequestFactory
from django.urls import reverse

from fighthealthinsurance.models import Denial
from fighthealthinsurance.views import RecommendAppeal


class InvalidFormPostNo500Test(TestCase):
    """Malformed POSTs should redirect, never raise a 500."""

    def setUp(self):
        self.client = Client()
        self.email = "tester@example.com"
        self.denial = Denial.objects.create(
            denial_text="denial body",
            hashed_email=Denial.get_hashed_email(self.email),
            insurance_company="Aetna",
            your_state="CA",
        )

    def test_share_appeal_invalid_form_redirects(self):
        """Missing required fields -> redirect, not a 500."""
        response = self.client.post(reverse("share_appeal"), {})
        self.assertEqual(response.status_code, 302)

    def test_share_appeal_unknown_denial_redirects(self):
        """Valid form but wrong denial_id/email combo -> redirect, not a 500."""
        response = self.client.post(
            reverse("share_appeal"),
            {
                "denial_id": 999999,
                "email": "nobody@example.com",
                "appeal_text": "Please reconsider.",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_choose_appeal_invalid_form_redirects(self):
        """Missing required fields -> redirect, not a 500."""
        response = self.client.post(reverse("choose_appeal"), {})
        self.assertEqual(response.status_code, 302)

    def test_generate_appeal_invalid_form_redirects(self):
        """Missing required fields -> redirect, not a 500."""
        response = self.client.post(reverse("generate_appeal"), {})
        self.assertEqual(response.status_code, 302)

    def test_generate_appeal_unknown_denial_redirects(self):
        """Valid form but wrong denial_id/email/semi_sekret -> redirect, not a 500."""
        response = self.client.post(
            reverse("generate_appeal"),
            {
                "denial_id": 999999,
                "email": "nobody@example.com",
                "semi_sekret": "wrong-sekret",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_followup_unknown_denial_404(self):
        """Expired/tampered follow-up link -> 404, not a 500."""
        response = self.client.get(
            reverse(
                "followup",
                kwargs={
                    "uuid": "00000000-0000-0000-0000-000000000000",
                    "hashed_email": "no-such-hash",
                    "follow_up_semi_sekret": "no-such-sekret",
                },
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_recommend_appeal_placeholder_not_implemented(self):
        """Placeholder view returns 501, not a 500 from an empty template name."""
        request = RequestFactory().post("/recommend_appeal")
        response = RecommendAppeal().post(request)
        self.assertEqual(response.status_code, 501)

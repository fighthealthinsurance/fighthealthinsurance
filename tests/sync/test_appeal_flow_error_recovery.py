"""The appeal-flow POST views must degrade to a redirect, never a 500.

GenerateAppeal.post and ChooseAppeal.post used to `return` None on an
invalid form (Django then raises "view didn't return an HttpResponse" -> 500),
and GenerateAppeal.post let Denial.DoesNotExist escape for a stale or
mismatched (denial_id, email, semi_sekret) triple. Both dead-ended the flow
at the exact step where the user was about to generate or finalize an appeal.
The escalation views already redirect to scan in these cases; these tests pin
the same recovery for the appeal views.
"""

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import models


class TestGenerateAppealPostRecovery(TestCase):
    def setUp(self):
        self.client = Client()

    def test_invalid_form_redirects_to_scan(self):
        response = self.client.post(reverse("generate_appeal"), {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("scan"))

    def test_mismatched_denial_ref_redirects_to_scan(self):
        denial = models.Denial.objects.create(
            denial_id=4242,
            semi_sekret="sekret",
            hashed_email=models.Denial.get_hashed_email("owner@example.com"),
        )
        response = self.client.post(
            reverse("generate_appeal"),
            {
                "denial_id": denial.denial_id,
                "email": "someone-else@example.com",
                "semi_sekret": denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("scan"))


class TestChooseAppealPostRecovery(TestCase):
    def setUp(self):
        self.client = Client()

    def test_missing_appeal_text_redirects_to_scan(self):
        # A user can clear the appeal textarea before submitting; appeal_text
        # is required, so the form is invalid.
        response = self.client.post(
            reverse("choose_appeal"),
            {
                "denial_id": 1,
                "email": "user@example.com",
                "semi_sekret": "sekret",
                "appeal_text": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("scan"))

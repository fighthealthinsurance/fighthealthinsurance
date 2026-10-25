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


class TestChooseAppealEmptyLetterKeepsTheCase(TestCase):
    """Emptying the letter before choosing it sends a real case back to its
    drafts. It used to land on the upload page, as if the appeal were gone."""

    EMAIL = "owner@example.com"

    def setUp(self):
        self.client = Client()
        self.denial = models.Denial.objects.create(
            denial_id=4343,
            denial_text="denied",
            semi_sekret="sekret",
            hashed_email=models.Denial.get_hashed_email(self.EMAIL),
        )

    def choose(self, appeal_text="", **overrides):
        data = {
            "denial_id": self.denial.denial_id,
            "email": self.EMAIL,
            "semi_sekret": self.denial.semi_sekret,
            "appeal_text": appeal_text,
        }
        data.update(overrides)
        return self.client.post(reverse("choose_appeal"), data)

    def assertBackToTheAppealsPage(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.url.startswith(reverse("generate_appeal") + "?ref="),
            response.url,
        )

    def test_an_empty_letter_goes_back_to_the_appeals_page(self):
        self.assertBackToTheAppealsPage(self.choose(""))

    def test_a_letter_of_only_spaces_goes_back_to_the_appeals_page(self):
        self.assertBackToTheAppealsPage(self.choose("   \n  "))

    def test_the_appeals_page_it_goes_back_to_opens_the_same_case(self):
        page = self.client.get(self.choose("").url)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "appeals.html")
        self.assertEqual(str(page.context["denial_id"]), str(self.denial.denial_id))

    def test_the_way_back_carries_no_email_or_secret(self):
        url = self.choose("").url
        self.assertNotIn("owner", url)
        self.assertNotIn(self.denial.semi_sekret, url)

    def test_a_case_with_another_email_still_goes_to_scan(self):
        response = self.choose("", email="someone-else@example.com")
        self.assertEqual(response.url, reverse("scan"))

    def test_a_case_with_the_wrong_secret_still_goes_to_scan(self):
        response = self.choose("", semi_sekret="not-the-secret")
        self.assertEqual(response.url, reverse("scan"))

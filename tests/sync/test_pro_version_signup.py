"""Tests for the /pro_version interest signup flow."""

from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.models import InterestedProfessional


class ProVersionSignupSuccessTest(TestCase):
    """Happy-path /pro_version signup.

    setUp performs the canonical POST once per test so each assertion stays
    focused on a single behavior.
    """

    PAYLOAD = {
        "name": "Jane Doe",
        "email": "jane@clinic.example",
        "job_title_or_provider_type": "Appeal specialist",
        "business_name": "Example Clinic",
        "phone_number": "555-0100",
        "address": "",
        "most_common_denial": "Not medically necessary",
        "comments": "Looking forward to it!",
    }

    def setUp(self):
        self.response = self.client.post(reverse("pro_version"), self.PAYLOAD)
        self.pro = InterestedProfessional.objects.get(email=self.PAYLOAD["email"])

    def _team_email(self):
        subject = f"New pro version signup #{self.pro.id}"
        return next(m for m in mail.outbox if m.subject == subject)

    def test_redirects_to_thankyou_page(self):
        self.assertRedirects(self.response, reverse("pro_version_thankyou"))

    def test_creates_interested_professional_record(self):
        self.assertEqual(self.pro.name, "Jane Doe")

    def test_marks_thankyou_email_sent(self):
        # ThankyouEmailSender.dosend() flips this on a successful send.
        self.assertTrue(self.pro.thankyou_email_sent)

    def test_records_clicked_for_paid_as_false(self):
        # Pay-to-express-interest is no longer collected.
        self.assertFalse(self.pro.clicked_for_paid)

    def test_sends_team_notification_to_support(self):
        self.assertEqual(self._team_email().to, ["support42@fighthealthinsurance.com"])

    def test_team_notification_includes_captured_fields(self):
        body = self._team_email().body
        for value in (
            "Jane Doe",
            "jane@clinic.example",
            "Appeal specialist",
            "Example Clinic",
            "555-0100",
            "Not medically necessary",
            "Looking forward to it!",
        ):
            self.assertIn(value, body)

    def test_sends_thankyou_email_to_signer(self):
        self.assertTrue(
            any(
                "Thanks for your interest" in m.subject
                and "jane@clinic.example" in m.to
                for m in mail.outbox
            )
        )


class ProVersionSignupEdgeCaseTest(TestCase):
    """Rendering, failure-mode, and security guards for the signup form."""

    def test_get_renders_signup_form(self):
        response = self.client.get(reverse("pro_version"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Updated Professional Version Coming Soon")
        self.assertContains(response, 'method="post"')
        # Pay-to-express-interest has been dropped.
        self.assertNotContains(response, "Pay $10")
        self.assertNotContains(response, "clicked_for_paid")

    def test_team_notification_failure_does_not_block_signup(self):
        with patch(
            "fighthealthinsurance.views.send_mail", side_effect=Exception("smtp down")
        ):
            response = self.client.post(
                reverse("pro_version"),
                {"name": "Resilient", "email": "resilient@clinic.example"},
            )
        self.assertRedirects(response, reverse("pro_version_thankyou"))
        self.assertTrue(
            InterestedProfessional.objects.filter(
                email="resilient@clinic.example"
            ).exists()
        )

    def test_post_rejects_mass_assignment_of_internal_fields(self):
        """Public submitters can't preset thankyou_email_sent/paid via POST."""
        # Patch out the immediate thank-you sender so the only path that could
        # flip thankyou_email_sent is the POST body itself — which Meta.exclude
        # must block. Without this patch, dosend() would always set it to True
        # and we couldn't distinguish a real save-path flip from a leaked POST.
        with patch("fighthealthinsurance.views.ThankyouEmailSender.dosend"):
            self.client.post(
                reverse("pro_version"),
                {
                    "name": "Hacky",
                    "email": "hacky@clinic.example",
                    "thankyou_email_sent": "true",
                    "paid": "true",
                },
            )
        pro = InterestedProfessional.objects.get(email="hacky@clinic.example")
        self.assertFalse(pro.thankyou_email_sent)
        self.assertFalse(pro.paid)

    def test_post_enforces_csrf(self):
        """The POST endpoint must enforce CSRF — it now writes to the DB."""
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("pro_version"),
            {"name": "CSRFless", "email": "csrfless@clinic.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            InterestedProfessional.objects.filter(
                email="csrfless@clinic.example"
            ).exists()
        )


@override_settings(PRO_VERSION_AVAILABLE=True)
class ProVersionAvailableModeTest(TestCase):
    """When the flag is on, /pro_version shows the available-now page (no form)."""

    def test_get_renders_available_page(self):
        response = self.client.get(reverse("pro_version"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The New Professional Version Is Here")
        self.assertContains(response, "Schedule a Trial or Learn More")
        self.assertNotContains(response, 'method="post"')

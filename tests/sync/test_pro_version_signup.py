"""Tests for the /pro_version interest signup flow."""

from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.models import InterestedProfessional


class ProVersionSignupTest(TestCase):
    """Verify the coming-soon /pro_version form captures interest and triggers emails."""

    def test_get_renders_signup_form(self):
        response = self.client.get(reverse("pro_version"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Updated Professional Version Coming Soon")
        self.assertContains(response, 'method="post"')
        # Pay-to-express-interest has been dropped.
        self.assertNotContains(response, "Pay $10")
        self.assertNotContains(response, "clicked_for_paid")

    def test_post_creates_interested_professional_and_sends_emails(self):
        response = self.client.post(
            reverse("pro_version"),
            {
                "name": "Jane Doe",
                "email": "jane@clinic.example",
                "job_title_or_provider_type": "Appeal specialist",
                "business_name": "Example Clinic",
                "phone_number": "555-0100",
                "address": "",
                "most_common_denial": "Not medically necessary",
                "comments": "Looking forward to it!",
            },
        )

        self.assertRedirects(response, reverse("pro_version_thankyou"))
        pro = InterestedProfessional.objects.get(email="jane@clinic.example")
        self.assertEqual(pro.name, "Jane Doe")
        # ThankyouEmailSender.dosend() flips this on a successful send.
        self.assertTrue(pro.thankyou_email_sent)
        # Pay-to-express-interest is no longer collected.
        self.assertFalse(pro.clicked_for_paid)

        subjects = [m.subject for m in mail.outbox]
        # Team notification to support42@
        self.assertIn(f"New pro version signup #{pro.id}", subjects)
        team_email = next(
            m for m in mail.outbox if m.subject == f"New pro version signup #{pro.id}"
        )
        self.assertEqual(team_email.to, ["support42@fighthealthinsurance.com"])
        self.assertIn("jane@clinic.example", team_email.body)
        self.assertIn("Jane Doe", team_email.body)

        # Immediate thank-you email to the signer
        self.assertTrue(
            any(
                "Thank you for signing up" in m.subject
                and "jane@clinic.example" in m.to
                for m in mail.outbox
            )
        )

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


@override_settings(PRO_VERSION_AVAILABLE=True)
class ProVersionAvailableModeTest(TestCase):
    """When the flag is on, /pro_version shows the available-now page (no form)."""

    def test_get_renders_available_page(self):
        response = self.client.get(reverse("pro_version"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The New Professional Version Is Here")
        self.assertContains(response, "Schedule a Trial or Learn More")
        self.assertNotContains(response, 'method="post"')

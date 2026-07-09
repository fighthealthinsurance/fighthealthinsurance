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

    def test_sends_team_notification_to_support_and_professional_inboxes(self):
        self.assertEqual(
            self._team_email().to,
            [
                "support42@fighthealthinsurance.com",
                "professional@fighthealthinsurance.com",
            ],
        )

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


class ProVersionSignupInvalidSubmissionTest(TestCase):
    """A submission missing the required email is rejected, not saved.

    Email is the only required field on the interest form, so an otherwise
    empty POST exercises the form_invalid path -- which must re-render the
    page (with the per-field error markup the template emits) instead of
    redirecting, creating a record, or sending any mail.
    """

    def setUp(self):
        self.response = self.client.post(
            reverse("pro_version"), {"name": "No Email", "email": ""}
        )

    def test_does_not_redirect(self):
        # form_invalid re-renders in place rather than redirecting to thankyou.
        self.assertEqual(self.response.status_code, 200)

    def test_rerenders_form_with_field_error(self):
        # Confirms the template surfaces validation errors to the user.
        self.assertContains(self.response, "This field is required.")

    def test_creates_no_record(self):
        self.assertFalse(InterestedProfessional.objects.exists())

    def test_sends_no_email(self):
        # Neither the team notification nor the thank-you fire on a rejected
        # submission (both live on the form_valid path).
        self.assertEqual(mail.outbox, [])


class ProVersionSignupEdgeCaseTest(TestCase):
    """Rendering, failure-mode, and security guards for the signup form."""

    def test_get_renders_signup_form(self):
        response = self.client.get(reverse("pro_version"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Updated Professional Version Available")
        self.assertContains(response, 'method="post"')
        # Pay-to-express-interest has been dropped.
        self.assertNotContains(response, "Pay $10")
        self.assertNotContains(response, "clicked_for_paid")

    def test_team_notification_failure_does_not_block_signup(self):
        with patch(
            "fighthealthinsurance.utils.send_mail", side_effect=Exception("smtp down")
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

    def test_repeat_submission_dedups_by_email(self):
        """A returning email reuses its existing lead: no duplicate row and no
        second thank-you, though the submitter still lands on the thankyou page."""
        payload = {"name": "Returning", "email": "returning@clinic.example"}
        first = self.client.post(reverse("pro_version"), payload)
        second = self.client.post(reverse("pro_version"), payload)
        self.assertRedirects(first, reverse("pro_version_thankyou"))
        self.assertRedirects(second, reverse("pro_version_thankyou"))
        self.assertEqual(
            InterestedProfessional.objects.filter(
                email="returning@clinic.example"
            ).count(),
            1,
        )
        thankyous = [
            m
            for m in mail.outbox
            if "returning@clinic.example" in m.to
            and "Thanks for your interest" in m.subject
        ]
        self.assertEqual(len(thankyous), 1)

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


class ExternalProSignupTest(TestCase):
    """The cross-origin classic-form intake used by the static Fight Paperwork
    site (fightpaperwork.com) POSTing to /pro_version_signup.

    Unlike /pro_version this endpoint is csrf_exempt (the static page can't get
    a token) and always 302-redirects rather than re-rendering, so behavior is
    asserted via the redirect target, the DB, and the outbox.
    """

    URL_NAME = "pro_version_external_signup"

    PAYLOAD = {
        "name": "Ext Pro",
        "email": "ext@clinic.example",
        "job_title_or_provider_type": "Biller",
        "business_name": "Static Clinic",
        "phone_number": "555-0199",
        "most_common_denial": "Prior auth",
        "comments": "Sent from fightpaperwork.com",
    }

    def test_valid_submission_redirects_to_thankyou(self):
        response = self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertRedirects(
            response,
            reverse("pro_version_thankyou"),
            fetch_redirect_response=False,
        )

    def test_valid_submission_creates_lead_and_notifies(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        pro = InterestedProfessional.objects.get(email="ext@clinic.example")
        self.assertEqual(pro.business_name, "Static Clinic")
        self.assertFalse(pro.clicked_for_paid)
        # Team notification names the off-site source in its subject.
        self.assertTrue(
            any(
                m.subject == f"New Fight Paperwork pro signup #{pro.id}"
                and m.to
                == [
                    "support42@fighthealthinsurance.com",
                    "professional@fighthealthinsurance.com",
                ]
                for m in mail.outbox
            )
        )

    def test_valid_submission_sends_thankyou_to_signer(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertTrue(
            any(
                "Thanks for your interest" in m.subject and "ext@clinic.example" in m.to
                for m in mail.outbox
            )
        )

    def test_accepts_post_without_csrf_token(self):
        """The static site can't obtain a CSRF token, so the endpoint must not
        enforce one (contrast with /pro_version which does)."""
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            InterestedProfessional.objects.filter(email="ext@clinic.example").exists()
        )

    def test_honeypot_submission_is_dropped_but_looks_successful(self):
        """A filled honeypot marks a bot: land on thankyou, save nothing, mail
        nothing."""
        payload = {**self.PAYLOAD, "website": "http://spam.example"}
        response = self.client.post(reverse(self.URL_NAME), payload)
        self.assertRedirects(
            response,
            reverse("pro_version_thankyou"),
            fetch_redirect_response=False,
        )
        self.assertFalse(InterestedProfessional.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_honeypot_wins_even_when_required_fields_missing(self):
        """The honeypot is checked before validation, so a bot can't dodge the
        trap by also omitting the required email: it still lands on thankyou
        (not the on-site form) and nothing is saved."""
        response = self.client.post(
            reverse(self.URL_NAME),
            {"name": "Bot", "email": "", "website": "http://spam.example"},
        )
        self.assertRedirects(
            response,
            reverse("pro_version_thankyou"),
            fetch_redirect_response=False,
        )
        self.assertFalse(InterestedProfessional.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_invalid_submission_redirects_to_onsite_form(self):
        """Missing the required email can't be re-rendered across origins, so
        the visitor is bounced to the on-site /pro_version form; nothing saved."""
        response = self.client.post(
            reverse(self.URL_NAME), {"name": "No Email", "email": ""}
        )
        self.assertRedirects(
            response, reverse("pro_version"), fetch_redirect_response=False
        )
        self.assertFalse(InterestedProfessional.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_get_redirects_to_onsite_form(self):
        response = self.client.get(reverse(self.URL_NAME))
        self.assertRedirects(
            response, reverse("pro_version"), fetch_redirect_response=False
        )

    def test_repeat_submission_dedups_by_email_case_insensitively(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.client.post(
            reverse(self.URL_NAME),
            {**self.PAYLOAD, "email": "EXT@clinic.example"},
        )
        self.assertEqual(
            InterestedProfessional.objects.filter(
                email__iexact="ext@clinic.example"
            ).count(),
            1,
        )
        thankyous = [
            m
            for m in mail.outbox
            if "Thanks for your interest" in m.subject
            and any("ext@clinic.example" in t.lower() for t in m.to)
        ]
        self.assertEqual(len(thankyous), 1)

    def test_rejects_mass_assignment_of_internal_fields(self):
        with patch("fighthealthinsurance.views.ThankyouEmailSender.dosend"):
            self.client.post(
                reverse(self.URL_NAME),
                {**self.PAYLOAD, "paid": "true", "thankyou_email_sent": "true"},
            )
        pro = InterestedProfessional.objects.get(email="ext@clinic.example")
        self.assertFalse(pro.paid)
        self.assertFalse(pro.thankyou_email_sent)

    def test_rejects_mass_assignment_of_proconnector_state(self):
        """A crafted POST must not preset pro-connector workflow state. If it
        could set proconnector_attempted/skipped=True, the lead would be born
        already-handled and proconnector.processable_queryset() would skip it,
        silently dropping a real lead from the outreach queue."""
        self.client.post(
            reverse(self.URL_NAME),
            {
                **self.PAYLOAD,
                "proconnector_attempted": "true",
                "proconnector_skipped": "true",
                "proconnector_skip_reason": "injected",
            },
        )
        pro = InterestedProfessional.objects.get(email="ext@clinic.example")
        self.assertFalse(pro.proconnector_attempted)
        self.assertFalse(pro.proconnector_skipped)
        self.assertIsNone(pro.proconnector_skip_reason)


class ProVersionThankYouDirectPostTest(TestCase):
    """Direct classic-form POSTs to /pro_version_thankyou.

    Older deployed/cached copies of the static Fight Paperwork interest form
    submit straight to the thank-you URL (which is csrf_exempt in the URLconf
    for exactly this reason). These posts must persist the lead and fan out
    the notification + thank-you emails instead of 405-ing and silently
    dropping the signup — the regression this class guards against.
    """

    URL_NAME = "pro_version_thankyou"

    PAYLOAD = {
        "name": "Direct Poster",
        "email": "direct@clinic.example",
        "job_title_or_provider_type": "Office manager",
        "business_name": "Legacy Clinic",
        "phone_number": "555-0142",
        "most_common_denial": "Out of network",
        "comments": "Posted straight to the thank-you page",
    }

    def test_get_still_renders_thankyou_page(self):
        response = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you for believing in us")

    def test_post_redirects_to_thankyou_page(self):
        # POST-redirect-GET back to the same page, matching the external
        # endpoint's contract (and refreshing won't re-submit).
        response = self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertRedirects(response, reverse(self.URL_NAME))

    def test_post_creates_lead(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        pro = InterestedProfessional.objects.get(email="direct@clinic.example")
        self.assertEqual(pro.business_name, "Legacy Clinic")
        self.assertFalse(pro.clicked_for_paid)

    def test_post_notifies_support_and_professional_inboxes(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        pro = InterestedProfessional.objects.get(email="direct@clinic.example")
        notification = next(
            m for m in mail.outbox if m.subject == f"New pro version signup #{pro.id}"
        )
        self.assertEqual(
            notification.to,
            [
                "support42@fighthealthinsurance.com",
                "professional@fighthealthinsurance.com",
            ],
        )
        # The body names the intake path so the team can tell a legacy
        # direct post apart from the on-site form.
        self.assertIn("/pro_version_thankyou", notification.body)

    def test_post_sends_thankyou_email_to_signer(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertTrue(
            any(
                "Thanks for your interest" in m.subject
                and "direct@clinic.example" in m.to
                for m in mail.outbox
            )
        )

    def test_accepts_post_without_csrf_token(self):
        """Cross-origin static pages can't obtain a CSRF token, so the
        endpoint must not enforce one."""
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            InterestedProfessional.objects.filter(
                email="direct@clinic.example"
            ).exists()
        )

    def test_honeypot_submission_is_dropped_but_looks_successful(self):
        payload = {**self.PAYLOAD, "website": "http://spam.example"}
        response = self.client.post(reverse(self.URL_NAME), payload)
        self.assertRedirects(response, reverse(self.URL_NAME))
        self.assertFalse(InterestedProfessional.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_invalid_submission_redirects_to_onsite_form(self):
        response = self.client.post(
            reverse(self.URL_NAME), {"name": "No Email", "email": ""}
        )
        self.assertRedirects(
            response, reverse("pro_version"), fetch_redirect_response=False
        )
        self.assertFalse(InterestedProfessional.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_repeat_submission_dedups_by_email(self):
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.client.post(reverse(self.URL_NAME), self.PAYLOAD)
        self.assertEqual(
            InterestedProfessional.objects.filter(
                email__iexact="direct@clinic.example"
            ).count(),
            1,
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

    def test_post_returns_405(self):
        """POSTs must not be silently swallowed when the page is read-only."""
        response = self.client.post(
            reverse("pro_version"),
            {"name": "Hopeful", "email": "hopeful@clinic.example"},
        )
        self.assertEqual(response.status_code, 405)
        self.assertFalse(
            InterestedProfessional.objects.filter(
                email="hopeful@clinic.example"
            ).exists()
        )

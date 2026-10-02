import json
import pathlib

from django.test import TestCase, Client
from unittest.mock import patch, MagicMock

from fighthealthinsurance.models import StripeRecoveryInfo


class PWYWCheckoutTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_pwyw_checkout_zero_amount(self):
        """Test that zero amount returns success without creating a Stripe session."""
        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps({"amount": 0}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["message"], "Free usage - no payment needed")

    def _checkout(self, mock_stripe_create, body):
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session
        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps(body),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["url"], "https://checkout.stripe.com/test")
        mock_stripe_create.assert_called_once()
        return mock_stripe_create.call_args[1]

    @patch("stripe.checkout.Session.create")
    def test_a_payment_comes_back_to_the_thank_you_page(self, mock_stripe_create):
        """Stripe opens in its own tab and comes back to a page that can be
        loaded with a GET. It used to come back to the page the person
        left, and the appeal letter page answers only POST, so the tab
        showed a blank 405 that a reload repeated."""
        call_kwargs = self._checkout(mock_stripe_create, {"amount": 10})
        self.assertEqual(call_kwargs["mode"], "payment")
        self.assertTrue(
            call_kwargs["success_url"].endswith("/pwyw/thanks?donation=success")
        )

    @patch("stripe.checkout.Session.create")
    def test_a_cancelled_payment_comes_back_to_the_thank_you_page(
        self, mock_stripe_create
    ):
        call_kwargs = self._checkout(mock_stripe_create, {"amount": 10})
        self.assertTrue(
            call_kwargs["cancel_url"].endswith("/pwyw/thanks?donation=cancelled")
        )

    @patch("stripe.checkout.Session.create")
    def test_a_return_url_from_an_older_page_is_ignored(self, mock_stripe_create):
        """A page cached from before this change still sends its own path;
        the checkout comes back to the thank-you page all the same."""
        call_kwargs = self._checkout(
            mock_stripe_create, {"amount": 25, "return_url": "/choose_appeal"}
        )
        self.assertNotIn("choose_appeal", call_kwargs["success_url"])
        self.assertNotIn("choose_appeal", call_kwargs["cancel_url"])

    @patch("stripe.checkout.Session.create")
    def test_an_absolute_return_url_never_reaches_stripe(self, mock_stripe_create):
        call_kwargs = self._checkout(
            mock_stripe_create,
            {"amount": 10, "return_url": "https://evil.com/phishing"},
        )
        self.assertNotIn("evil.com", call_kwargs["success_url"])
        self.assertNotIn("evil.com", call_kwargs["cancel_url"])

    @patch("stripe.checkout.Session.create")
    def test_pwyw_checkout_persists_recovery_info(self, mock_stripe_create):
        """The donation checkout stores recovery info so an expired link can rebuild it."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps({"amount": 30}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        call_kwargs = mock_stripe_create.call_args[1]
        # A StripeRecoveryInfo holding the line items must be created and
        # referenced in the metadata so CompletePaymentView can rebuild the
        # session from the recovery email link.
        recovery_info_id = call_kwargs["metadata"]["recovery_info_id"]
        recovery_info = StripeRecoveryInfo.objects.get(id=recovery_info_id)
        self.assertEqual(recovery_info.items, call_kwargs["line_items"])
        self.assertEqual(recovery_info.items[0]["price_data"]["unit_amount"], 30 * 100)

    @patch("stripe.checkout.Session.create")
    def test_pwyw_checkout_stripe_error(self, mock_stripe_create):
        """Test error handling when Stripe API fails."""
        mock_stripe_create.side_effect = Exception("Stripe API error")

        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps({"amount": 10}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.content)
        self.assertFalse(data["success"])
        self.assertIn("error", data)


class PWYWThanksPageTest(TestCase):
    def test_the_thank_you_page_loads_with_a_get(self):
        response = self.client.get("/pwyw/thanks?donation=success")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")
        self.assertContains(response, "still open in your other tab")

    def test_the_thank_you_page_carries_no_case_id(self):
        """A visitor partway through an appeal has its id in the session;
        base.html would put it in a meta tag on any page that keeps it."""
        session = self.client.session
        session["denial_uuid"] = "11111111-2222-3333-4444-555555555555"
        session.save()
        response = self.client.get("/pwyw/thanks?donation=success")
        self.assertNotContains(response, "11111111-2222-3333-4444-555555555555")
        self.assertNotContains(response, "fhi-session-key")

    def test_the_page_says_nothing_was_charged_after_a_cancel(self):
        response = self.client.get("/pwyw/thanks?donation=cancelled")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nothing was charged")


APPEAL_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "templates"
    / "appeal.html"
).read_text()


class AppealPageAsksOnceTest(TestCase):
    """The appeal page asked for a donation four times: two pay-what-you-want
    panels and two buttons straight to a Stripe Payment Link. It asks once,
    after the person has sent their appeal."""

    def test_the_appeal_page_has_one_pay_what_you_want_panel(self):
        self.assertEqual(APPEAL_TEMPLATE.count("partials/pwyw_panel.html"), 1)

    def test_the_appeal_page_links_to_no_payment_page_of_its_own(self):
        self.assertNotIn("buy.stripe.com", APPEAL_TEMPLATE)

import json
from django.test import TestCase, Client
from unittest.mock import patch, MagicMock


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

    @patch("stripe.checkout.Session.create")
    def test_pwyw_checkout_with_amount_no_return_url(self, mock_stripe_create):
        """Test creating checkout session with amount but no return URL."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps({"amount": 10}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["url"], "https://checkout.stripe.com/test")

        # Verify Stripe session was created with correct parameters
        mock_stripe_create.assert_called_once()
        call_kwargs = mock_stripe_create.call_args[1]
        self.assertEqual(call_kwargs["mode"], "payment")
        self.assertTrue(call_kwargs["success_url"].endswith("/?donation=success"))
        self.assertTrue(call_kwargs["cancel_url"].endswith("/"))

    @patch("stripe.checkout.Session.create")
    def test_pwyw_checkout_with_return_url(self, mock_stripe_create):
        """Test creating checkout session with a return URL."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps({"amount": 25, "return_url": "/appeal"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["url"], "https://checkout.stripe.com/test")

        # Verify Stripe session was created with return URL
        mock_stripe_create.assert_called_once()
        call_kwargs = mock_stripe_create.call_args[1]
        self.assertTrue(call_kwargs["success_url"].endswith("/appeal?donation=success"))
        self.assertTrue(call_kwargs["cancel_url"].endswith("/appeal"))

    @patch("stripe.checkout.Session.create")
    def test_pwyw_checkout_with_return_url_with_query_params(self, mock_stripe_create):
        """Test creating checkout session with a return URL that has query params."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            "/v0/pwyw/checkout",
            data=json.dumps({"amount": 15, "return_url": "/appeal?foo=bar"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])

        # Verify Stripe session was created with correct URL handling
        mock_stripe_create.assert_called_once()
        call_kwargs = mock_stripe_create.call_args[1]
        # Should append donation=success with & since URL already has params
        self.assertTrue(call_kwargs["success_url"].endswith("/appeal?foo=bar&donation=success"))
        self.assertTrue(call_kwargs["cancel_url"].endswith("/appeal?foo=bar"))

    def test_pwyw_checkout_rejects_absolute_return_url(self):
        """Test that absolute URLs in return_url are rejected for security."""
        # Mock stripe to avoid actual API calls
        with patch("stripe.checkout.Session.create") as mock_stripe_create:
            mock_session = MagicMock()
            mock_session.url = "https://checkout.stripe.com/test"
            mock_stripe_create.return_value = mock_session

            response = self.client.post(
                "/v0/pwyw/checkout",
                data=json.dumps(
                    {"amount": 10, "return_url": "https://evil.com/phishing"}
                ),
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 200)
            # Should ignore the malicious URL and use default
            call_kwargs = mock_stripe_create.call_args[1]
            self.assertTrue(call_kwargs["success_url"].endswith("/?donation=success"))
            self.assertFalse("evil.com" in call_kwargs["success_url"])

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

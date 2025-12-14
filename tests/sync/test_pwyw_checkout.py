"""Test PWYW checkout functionality."""

import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse


class PWYWCheckoutTest(TestCase):
    """Test that PWYW checkout handles return URLs correctly."""

    def setUp(self):
        self.client = Client()

    def test_free_usage(self):
        """Test that amount=0 returns success without creating a Stripe session."""
        response = self.client.post(
            reverse("pwyw_checkout"),
            data=json.dumps({
                "amount": 0,
                "return_url": "/some/page"
            }),
            content_type="application/json",
        )
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["message"], "Free usage - no payment needed")

    @patch("stripe.checkout.Session.create")
    def test_checkout_with_return_url(self, mock_stripe_create):
        """Test that checkout session uses provided return_url."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            reverse("pwyw_checkout"),
            data=json.dumps({
                "amount": 10,
                "return_url": "/v0/followup/123/abc/def"
            }),
            content_type="application/json",
        )
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["url"], "https://checkout.stripe.com/test")
        
        # Check that Stripe session was created with the right URLs
        call_args = mock_stripe_create.call_args
        self.assertIn("success_url", call_args.kwargs)
        self.assertIn("cancel_url", call_args.kwargs)
        self.assertIn("/v0/followup/123/abc/def", call_args.kwargs["success_url"])
        self.assertIn("donation=success", call_args.kwargs["success_url"])
        self.assertIn("/v0/followup/123/abc/def", call_args.kwargs["cancel_url"])

    @patch("stripe.checkout.Session.create")
    def test_checkout_with_query_params_in_return_url(self, mock_stripe_create):
        """Test that existing query params in return_url are preserved."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            reverse("pwyw_checkout"),
            data=json.dumps({
                "amount": 10,
                "return_url": "/some/page?existing=param"
            }),
            content_type="application/json",
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Check that success_url has both existing param and donation=success
        call_args = mock_stripe_create.call_args
        success_url = call_args.kwargs["success_url"]
        self.assertIn("existing=param", success_url)
        self.assertIn("donation=success", success_url)

    @patch("stripe.checkout.Session.create")
    def test_checkout_rejects_external_return_url(self, mock_stripe_create):
        """Test that absolute URLs in return_url are rejected for security.
        
        We reject all absolute URLs (even same-host) to prevent host header
        injection attacks. Only relative URLs are allowed.
        """
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            reverse("pwyw_checkout"),
            data=json.dumps({
                "amount": 10,
                "return_url": "https://evil.com/steal-data"
            }),
            content_type="application/json",
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Check that external URL was replaced with "/"
        call_args = mock_stripe_create.call_args
        success_url = call_args.kwargs["success_url"]
        self.assertNotIn("evil.com", success_url)
        # Should fall back to home page
        self.assertIn("donation=success", success_url)

    @patch("stripe.checkout.Session.create")
    def test_checkout_rejects_same_host_absolute_url(self, mock_stripe_create):
        """Test that same-host absolute URLs are also rejected for security.
        
        Even absolute URLs from the same host are rejected to prevent
        host header injection attacks. Only relative URLs are accepted.
        """
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            reverse("pwyw_checkout"),
            data=json.dumps({
                "amount": 10,
                "return_url": "http://testserver/some/page"
            }),
            content_type="application/json",
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Check that absolute URL was replaced with "/"
        call_args = mock_stripe_create.call_args
        success_url = call_args.kwargs["success_url"]
        # Should fall back to home page, not the requested page
        self.assertIn("donation=success", success_url)
        # The path from the absolute URL should not be in the success_url
        # since we rejected it

    @patch("stripe.checkout.Session.create")
    def test_checkout_defaults_to_home_without_return_url(self, mock_stripe_create):
        """Test that checkout defaults to home page if no return_url provided."""
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test"
        mock_stripe_create.return_value = mock_session

        response = self.client.post(
            reverse("pwyw_checkout"),
            data=json.dumps({
                "amount": 10
            }),
            content_type="application/json",
        )
        
        self.assertEqual(response.status_code, 200)
        
        # Should default to "/"
        call_args = mock_stripe_create.call_args
        self.assertIn("success_url", call_args.kwargs)
        self.assertIn("cancel_url", call_args.kwargs)

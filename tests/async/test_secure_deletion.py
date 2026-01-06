"""Tests for secure data deletion with email confirmation."""

import datetime
from unittest.mock import Mock, patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

import pytest
from fighthealthinsurance.models import DataDeletionRequest, Denial


class DataDeletionRequestModelTest(TestCase):
    """Test DataDeletionRequest model functionality."""

    def test_create_deletion_request(self):
        """Test creating a deletion request."""
        request = DataDeletionRequest.objects.create(
            email="test@example.com",
            hashed_email="test-hash",
            token="test-token-123",
            ip_address="127.0.0.1",
        )

        self.assertEqual(request.email, "test@example.com")
        self.assertEqual(request.token, "test-token-123")
        self.assertFalse(request.confirmed)
        self.assertIsNone(request.confirmed_at)
        self.assertIsNotNone(request.created_at)

    def test_is_expired_returns_false_for_recent(self):
        """Test that recent requests are not expired."""
        request = DataDeletionRequest.objects.create(
            email="test@example.com",
            hashed_email="test-hash",
            token="test-token",
        )

        self.assertFalse(request.is_expired())

    def test_is_expired_returns_true_for_old(self):
        """Test that old requests are expired."""
        request = DataDeletionRequest.objects.create(
            email="test@example.com",
            hashed_email="test-hash",
            token="test-token",
        )

        # Manually set created_at to 25 hours ago
        request.created_at = timezone.now() - datetime.timedelta(hours=25)
        request.save()

        self.assertTrue(request.is_expired())

    def test_token_uniqueness(self):
        """Test that tokens must be unique."""
        DataDeletionRequest.objects.create(
            email="test1@example.com",
            hashed_email="hash1",
            token="same-token",
        )

        # Should raise IntegrityError for duplicate token
        with pytest.raises(Exception):  # IntegrityError from DB
            DataDeletionRequest.objects.create(
                email="test2@example.com",
                hashed_email="hash2",
                token="same-token",
            )


class RemoveDataViewTest(TestCase):
    """Test the RemoveDataView for creating deletion requests."""

    def setUp(self):
        """Set up test client."""
        self.client = Client()
        self.url = reverse("remove_data")

    def test_remove_data_view_get(self):
        """Test GET request shows the form."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "email")

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_remove_data_view_post_creates_request(self, mock_send):
        """Test POST request creates deletion request."""
        mock_send.return_value = None

        response = self.client.post(self.url, {"email": "test@example.com"})

        # Should create a deletion request
        self.assertEqual(DataDeletionRequest.objects.count(), 1)

        deletion_request = DataDeletionRequest.objects.first()
        self.assertEqual(deletion_request.email, "test@example.com")
        self.assertFalse(deletion_request.confirmed)

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_remove_data_view_sends_confirmation_email(self, mock_send):
        """Test that confirmation email is sent."""
        mock_send.return_value = None

        self.client.post(self.url, {"email": "test@example.com"})

        # Email should have been sent
        mock_send.assert_called_once()
        call_args = mock_send.call_args

        # Check email content
        self.assertEqual(call_args.kwargs["to_email"], "test@example.com")
        self.assertIn("deletion", call_args.kwargs["subject"].lower())

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_remove_data_view_includes_confirmation_url(self, mock_send):
        """Test that confirmation email includes the confirmation URL."""
        mock_send.return_value = None

        self.client.post(self.url, {"email": "test@example.com"})

        call_args = mock_send.call_args
        context = call_args.kwargs["context"]

        self.assertIn("confirmation_url", context)
        self.assertIn("confirm_deletion", context["confirmation_url"])

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_remove_data_view_captures_ip_address(self, mock_send):
        """Test that IP address is captured for audit trail."""
        mock_send.return_value = None

        self.client.post(self.url, {"email": "test@example.com"})

        deletion_request = DataDeletionRequest.objects.first()
        self.assertIsNotNone(deletion_request.ip_address)


class ConfirmDataDeletionViewTest(TestCase):
    """Test the ConfirmDataDeletionView for processing deletions."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()

        # Create a test denial
        self.denial = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test",
            hashed_email="test-hash",
            raw_email="test@example.com",
        )

        # Create a valid deletion request
        self.deletion_request = DataDeletionRequest.objects.create(
            email="test@example.com",
            hashed_email="test-hash",
            token="valid-token-123",
        )

        self.url = reverse("confirm_deletion", kwargs={"token": "valid-token-123"})

    def test_confirm_deletion_get_shows_confirmation_page(self):
        """Test GET request shows confirmation page."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test@example.com")
        self.assertContains(response, "confirm")

    def test_confirm_deletion_get_with_invalid_token(self):
        """Test GET with invalid token shows error."""
        url = reverse("confirm_deletion", kwargs={"token": "invalid-token"})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid")

    def test_confirm_deletion_get_with_expired_token(self):
        """Test GET with expired token shows error."""
        # Create an expired request
        expired_request = DataDeletionRequest.objects.create(
            email="expired@example.com",
            hashed_email="expired-hash",
            token="expired-token",
        )
        expired_request.created_at = timezone.now() - datetime.timedelta(hours=25)
        expired_request.save()

        url = reverse("confirm_deletion", kwargs={"token": "expired-token"})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "expired")

    def test_confirm_deletion_get_with_already_confirmed(self):
        """Test GET with already confirmed token shows message."""
        self.deletion_request.confirmed = True
        self.deletion_request.save()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already")

    @patch(
        "fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.remove_data_for_email"
    )
    def test_confirm_deletion_post_deletes_data(self, mock_remove):
        """Test POST request actually deletes data."""
        mock_remove.return_value = None

        response = self.client.post(self.url)

        # Should call removal function
        mock_remove.assert_called_once_with("test@example.com")

        # Should mark as confirmed
        self.deletion_request.refresh_from_db()
        self.assertTrue(self.deletion_request.confirmed)
        self.assertIsNotNone(self.deletion_request.confirmed_at)

    @patch(
        "fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.remove_data_for_email"
    )
    def test_confirm_deletion_post_shows_success(self, mock_remove):
        """Test POST shows success page."""
        mock_remove.return_value = None

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "deleted")

    def test_confirm_deletion_post_with_invalid_token(self):
        """Test POST with invalid token shows error."""
        url = reverse("confirm_deletion", kwargs={"token": "invalid"})
        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid")

    def test_confirm_deletion_post_with_expired_token(self):
        """Test POST with expired token shows error."""
        expired_request = DataDeletionRequest.objects.create(
            email="expired@example.com",
            hashed_email="expired-hash",
            token="expired-token-2",
        )
        expired_request.created_at = timezone.now() - datetime.timedelta(hours=30)
        expired_request.save()

        url = reverse("confirm_deletion", kwargs={"token": "expired-token-2"})
        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "expired")

        # Should NOT be marked as confirmed
        expired_request.refresh_from_db()
        self.assertFalse(expired_request.confirmed)

    @patch(
        "fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.remove_data_for_email"
    )
    def test_confirm_deletion_post_idempotent(self, mock_remove):
        """Test that confirming twice doesn't cause issues."""
        mock_remove.return_value = None

        # First confirmation
        self.client.post(self.url)

        # Second confirmation
        response = self.client.post(self.url)

        # Should still show success (already deleted)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "deleted")

        # Should only call remove_data once
        self.assertEqual(mock_remove.call_count, 1)


class DeletionSecurityTest(TestCase):
    """Test security aspects of deletion process."""

    def test_token_is_random_and_secure(self):
        """Test that generated tokens are sufficiently random."""
        tokens = set()

        # Create multiple deletion requests
        for i in range(10):
            with patch("fighthealthinsurance.utils.send_fallback_email"):
                client = Client()
                client.post(reverse("remove_data"), {"email": f"test{i}@example.com"})

        requests = DataDeletionRequest.objects.all()

        for request in requests:
            # Token should be at least 20 characters (secure)
            self.assertGreaterEqual(len(request.token), 20)

            # All tokens should be unique
            self.assertNotIn(request.token, tokens)
            tokens.add(request.token)

    def test_deletion_requires_email_match(self):
        """Test that deletion only affects the specified email."""
        # Create denials for two different emails
        denial1 = Denial.objects.create(
            raw_email="user1@example.com",
            hashed_email="hash1",
            denial_text="Test 1",
        )

        denial2 = Denial.objects.create(
            raw_email="user2@example.com",
            hashed_email="hash2",
            denial_text="Test 2",
        )

        # Create deletion request for user1
        deletion_request = DataDeletionRequest.objects.create(
            email="user1@example.com",
            hashed_email="hash1",
            token="test-token",
        )

        # Confirm deletion
        with patch(
            "fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.remove_data_for_email"
        ) as mock_remove:
            mock_remove.return_value = None
            client = Client()
            url = reverse("confirm_deletion", kwargs={"token": "test-token"})
            client.post(url)

            # Should only call with user1's email
            mock_remove.assert_called_once_with("user1@example.com")

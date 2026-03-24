import datetime
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import (
    Denial,
    DeleteToken,
    MailingListSubscriber,
    UsedDeleteToken,
)


class TestRemoveDataView(TestCase):
    """Test that RemoveDataView sends confirmation email instead of deleting."""

    def _hashed(self, email):
        return Denial.get_hashed_email(email)

    def test_post_sends_confirmation_email(self):
        url = reverse("remove_data")
        response = self.client.post(url, {"email": "test@example.com"})
        assert response.status_code == 200
        assert b"Check Your Email" in response.content
        assert len(mail.outbox) >= 1
        assert mail.outbox[0].subject == "Confirm Data Deletion Request"
        assert "confirm-delete" in mail.outbox[0].body
        assert DeleteToken.objects.filter(
            hashed_email=self._hashed("test@example.com")
        ).exists()

    def test_post_replaces_existing_token(self):
        url = reverse("remove_data")
        hashed = self._hashed("test@example.com")
        self.client.post(url, {"email": "test@example.com"})
        first_token = DeleteToken.objects.get(hashed_email=hashed).token
        self.client.post(url, {"email": "test@example.com"})
        second_token = DeleteToken.objects.get(hashed_email=hashed).token
        assert DeleteToken.objects.filter(hashed_email=hashed).count() == 1
        assert first_token != second_token

    def test_get_shows_form(self):
        url = reverse("remove_data")
        response = self.client.get(url)
        assert response.status_code == 200
        assert b"Delete your Data" in response.content
        assert b"support42@fighthealthinsurance.com" in response.content

    def test_post_shows_support_email_note(self):
        url = reverse("remove_data")
        response = self.client.post(url, {"email": "test@example.com"})
        assert b"support42@fighthealthinsurance.com" in response.content

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_post_does_not_delete_data_immediately(self, mock_remove):
        """Ensure POST only sends email and does NOT delete data."""
        url = reverse("remove_data")
        self.client.post(url, {"email": "test@example.com"})
        mock_remove.assert_not_called()

    def test_confirmation_email_contains_correct_token(self):
        """Verify the email body contains the correct token and email params."""
        url = reverse("remove_data")
        self.client.post(url, {"email": "test@example.com"})
        hashed = self._hashed("test@example.com")
        token = DeleteToken.objects.get(hashed_email=hashed)
        email_body = mail.outbox[0].body
        assert str(token.token) in email_body
        assert "test%40example.com" in email_body or "test@example.com" in email_body

    def test_confirmation_email_contains_working_link(self):
        """Extract the link from the email and verify it loads the confirmation page."""
        import re
        from urllib.parse import parse_qs, urlparse

        url = reverse("remove_data")
        self.client.post(url, {"email": "test@example.com"})
        hashed = self._hashed("test@example.com")
        token = DeleteToken.objects.get(hashed_email=hashed)
        email_body = mail.outbox[0].body

        # Extract URL from email body
        urls = re.findall(r"https?://[^\s]+", email_body)
        assert len(urls) >= 1, "No URL found in email body"
        confirmation_url = urls[0]

        # Verify URL structure
        parsed = urlparse(confirmation_url)
        params = parse_qs(parsed.query)
        assert parsed.path == "/confirm-delete"
        assert params["token"] == [str(token.token)]
        assert params["email"] == ["test@example.com"]

        # Follow the link with the test client (use just path + query)
        response = self.client.get(
            f"{parsed.path}?{parsed.query}",
        )
        assert response.status_code == 200
        assert b"Confirm Data Deletion" in response.content
        assert b"Confirm Deletion" in response.content

    def test_delete_token_stores_hashed_email_not_plaintext(self):
        """Verify that DeleteToken stores a hashed email, not plaintext."""
        url = reverse("remove_data")
        self.client.post(url, {"email": "test@example.com"})
        token = DeleteToken.objects.first()
        assert token is not None
        # hashed_email should be a SHA-512 hex digest, not the raw email
        assert token.hashed_email != "test@example.com"
        assert token.hashed_email == self._hashed("test@example.com")
        assert not hasattr(token, "email") or "email" not in [
            f.name for f in token._meta.get_fields()
        ]


class TestConfirmDeleteDataView(TestCase):
    """Test the email confirmation link handler.

    GET renders a confirmation page; POST performs deletion.
    """

    def _hashed(self, email):
        return Denial.get_hashed_email(email)

    def _create_token(self, email="test@example.com", expired=False):
        token = DeleteToken(hashed_email=self._hashed(email))
        if expired:
            token.expires_at = timezone.now() - datetime.timedelta(hours=1)
        token.save()
        return token

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_get_valid_token_shows_confirmation_page(self, mock_remove):
        """GET with valid token shows confirmation form, does NOT delete."""
        token = self._create_token()
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"Confirm Data Deletion" in response.content
        assert b"Confirm Deletion" in response.content
        mock_remove.assert_not_called()
        assert DeleteToken.objects.filter(
            hashed_email=self._hashed("test@example.com")
        ).exists()

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_post_valid_token_deletes_data(self, mock_remove):
        """POST with valid token performs deletion."""
        token = self._create_token()
        url = reverse("confirm_delete_data")
        response = self.client.post(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"data associated with your email has been removed" in response.content
        mock_remove.assert_called_once_with("test@example.com")
        assert not DeleteToken.objects.filter(
            hashed_email=self._hashed("test@example.com")
        ).exists()

    def test_invalid_token_shows_error(self):
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": "bad-token", "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"Invalid confirmation link" in response.content

    def test_expired_token_shows_error(self):
        token = self._create_token(expired=True)
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"expired" in response.content
        assert not DeleteToken.objects.filter(
            hashed_email=self._hashed("test@example.com")
        ).exists()

    def test_missing_params_shows_error(self):
        url = reverse("confirm_delete_data")
        response = self.client.get(url)
        assert response.status_code == 200
        assert b"Invalid confirmation link" in response.content

    def test_wrong_email_for_token_shows_error(self):
        token = self._create_token(email="correct@example.com")
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": token.token, "email": "wrong@example.com"}
        )
        assert response.status_code == 200
        assert b"Invalid confirmation link" in response.content

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_already_used_token_shows_specific_message(self, mock_remove):
        """After using a token, trying it again shows 'already been used'."""
        token = self._create_token()
        url = reverse("confirm_delete_data")
        # Use the token
        response = self.client.post(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        token_value = token.token
        # Try the same token again (GET)
        response = self.client.get(
            url, {"token": token_value, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"already been used" in response.content
        # Try the same token again (POST)
        response = self.client.post(
            url, {"token": token_value, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"already been used" in response.content

    def test_invalid_token_does_not_say_already_used(self):
        """A completely fabricated token should say 'Invalid', not 'already been used'."""
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": "completely-fabricated-token", "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"Invalid confirmation link" in response.content
        assert b"already been used" not in response.content

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_used_token_record_created_after_deletion(self, mock_remove):
        """After POST deletion, a UsedDeleteToken record should exist."""
        token = self._create_token()
        url = reverse("confirm_delete_data")
        self.client.post(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert UsedDeleteToken.objects.filter(token=token.token).exists()
        used = UsedDeleteToken.objects.get(token=token.token)
        assert used.hashed_email == self._hashed("test@example.com")

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_email_case_insensitive_validation(self, mock_remove):
        """Token created with lowercase email should validate with mixed case."""
        token = self._create_token(email="test@example.com")
        url = reverse("confirm_delete_data")
        # GET with mixed case
        response = self.client.get(
            url, {"token": token.token, "email": "Test@Example.COM"}
        )
        assert response.status_code == 200
        assert b"Confirm Data Deletion" in response.content
        # POST with mixed case
        response = self.client.post(
            url, {"token": token.token, "email": "Test@Example.COM"}
        )
        assert response.status_code == 200
        assert b"data associated with your email has been removed" in response.content
        mock_remove.assert_called_once()

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_email_with_whitespace_normalization(self, mock_remove):
        """Token should validate even with leading/trailing whitespace in email."""
        token = self._create_token(email="test@example.com")
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": token.token, "email": "  test@example.com  "}
        )
        assert response.status_code == 200
        assert b"Confirm Data Deletion" in response.content

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_new_token_after_previous_deletion(self, mock_remove):
        """After deleting data, user can request and use a new token."""
        # First deletion
        token1 = self._create_token()
        url = reverse("confirm_delete_data")
        self.client.post(
            url, {"token": token1.token, "email": "test@example.com"}
        )
        assert UsedDeleteToken.objects.filter(token=token1.token).exists()
        # Request a new token (simulates RemoveDataView.post creating a new one)
        token2 = self._create_token(email="test@example.com")
        assert token2.token != token1.token
        # Use the new token
        response = self.client.post(
            url, {"token": token2.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"data associated with your email has been removed" in response.content

    def test_email_with_plus_sign_url_encoding(self):
        """Email with + sign should work end-to-end through URL encoding."""
        import re
        from urllib.parse import parse_qs, urlparse

        email = "user+tag@example.com"
        url = reverse("remove_data")
        self.client.post(url, {"email": email})
        hashed = self._hashed(email)
        token = DeleteToken.objects.get(hashed_email=hashed)
        email_body = mail.outbox[0].body

        # Extract URL from email
        urls = re.findall(r"https?://[^\s]+", email_body)
        assert len(urls) >= 1, "No URL found in email body"
        confirmation_url = urls[0]

        # Verify URL encodes + as %2B (not left as literal +)
        parsed = urlparse(confirmation_url)
        params = parse_qs(parsed.query)
        assert params["email"] == [email]

        # Follow the link
        response = self.client.get(f"{parsed.path}?{parsed.query}")
        assert response.status_code == 200
        assert b"Confirm Data Deletion" in response.content

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_double_post_second_returns_already_used(self, mock_remove):
        """Two rapid POSTs with the same token: second gets 'already used'."""
        token = self._create_token()
        url = reverse("confirm_delete_data")
        # First POST succeeds
        response1 = self.client.post(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response1.status_code == 200
        assert b"data associated with your email has been removed" in response1.content
        # Second POST with same token
        response2 = self.client.post(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response2.status_code == 200
        assert b"already been used" in response2.content

    def test_email_case_insensitive_integration(self):
        """Integration test: mixed-case stored records are deleted by case-insensitive lookup."""
        # Store a record with mixed-case email
        MailingListSubscriber.objects.create(
            email="Test@Example.COM", name="Test User"
        )
        assert MailingListSubscriber.objects.filter(email="Test@Example.COM").exists()

        # Create a token for the lowercase version of the email
        token = self._create_token(email="test@example.com")
        url = reverse("confirm_delete_data")

        # POST with lowercase email (as would arrive from the normalized confirmation link)
        response = self.client.post(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"data associated with your email has been removed" in response.content

        # The mixed-case record should have been deleted via __iexact lookup
        assert not MailingListSubscriber.objects.filter(
            email="Test@Example.COM"
        ).exists()

import datetime
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from fhi_users.models import DeleteToken


@pytest.mark.django_db
class TestRemoveDataView(TestCase):
    """Test that RemoveDataView sends confirmation email instead of deleting."""

    def test_post_sends_confirmation_email(self):
        url = reverse("remove_data")
        response = self.client.post(url, {"email": "test@example.com"})
        assert response.status_code == 200
        assert b"Check Your Email" in response.content
        # Email should be sent
        assert len(mail.outbox) >= 1
        assert mail.outbox[0].subject == "Confirm Data Deletion Request"
        assert "confirm-delete" in mail.outbox[0].body
        # Token should be created
        assert DeleteToken.objects.filter(email="test@example.com").exists()

    def test_post_replaces_existing_token(self):
        url = reverse("remove_data")
        # First request
        self.client.post(url, {"email": "test@example.com"})
        first_token = DeleteToken.objects.get(email="test@example.com").token
        # Second request
        self.client.post(url, {"email": "test@example.com"})
        second_token = DeleteToken.objects.get(email="test@example.com").token
        # Only one token should exist and it should be different
        assert DeleteToken.objects.filter(email="test@example.com").count() == 1
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


@pytest.mark.django_db
class TestConfirmDeleteDataView(TestCase):
    """Test the email confirmation link handler."""

    def _create_token(self, email="test@example.com", expired=False):
        token = DeleteToken(email=email)
        if expired:
            token.expires_at = timezone.now() - datetime.timedelta(hours=1)
        token.save()
        return token

    @patch("fighthealthinsurance.views.RemoveDataHelper.remove_data_for_email")
    def test_valid_token_deletes_data(self, mock_remove):
        token = self._create_token()
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"data associated with your email has been removed" in response.content
        mock_remove.assert_called_once_with("test@example.com")
        # Token should be cleaned up
        assert not DeleteToken.objects.filter(email="test@example.com").exists()

    def test_invalid_token_shows_error(self):
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": "bad-token", "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"Invalid or already used" in response.content

    def test_expired_token_shows_error(self):
        token = self._create_token(expired=True)
        url = reverse("confirm_delete_data")
        response = self.client.get(
            url, {"token": token.token, "email": "test@example.com"}
        )
        assert response.status_code == 200
        assert b"expired" in response.content
        # Expired token should be cleaned up
        assert not DeleteToken.objects.filter(email="test@example.com").exists()

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
        assert b"Invalid or already used" in response.content


@pytest.mark.django_db
class TestDataRemovalRestAPI(TestCase):
    """Test the REST API data removal endpoints."""

    def test_delete_sends_confirmation_email(self):
        url = reverse("dataremoval-list")
        response = self.client.delete(
            url,
            data={"email": "test@example.com"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert len(mail.outbox) >= 1
        assert mail.outbox[0].subject == "Confirm Data Deletion Request"
        assert DeleteToken.objects.filter(email="test@example.com").exists()

    @patch("fighthealthinsurance.rest_views.RemoveDataHelper.remove_data_for_email")
    def test_confirm_delete_with_valid_token(self, mock_remove):
        # Create a token first
        token = DeleteToken(email="test@example.com")
        token.save()

        url = reverse("dataremoval-confirm-delete")
        response = self.client.post(
            url,
            data={"token": str(token.token), "email": "test@example.com"},
            content_type="application/json",
        )
        assert response.status_code == 204
        mock_remove.assert_called_once_with("test@example.com")
        assert not DeleteToken.objects.filter(email="test@example.com").exists()

    def test_confirm_delete_with_invalid_token(self):
        url = reverse("dataremoval-confirm-delete")
        response = self.client.post(
            url,
            data={"token": "bad-token", "email": "test@example.com"},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_confirm_delete_with_expired_token(self):
        token = DeleteToken(email="test@example.com")
        token.expires_at = timezone.now() - datetime.timedelta(hours=1)
        token.save()

        url = reverse("dataremoval-confirm-delete")
        response = self.client.post(
            url,
            data={"token": str(token.token), "email": "test@example.com"},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert not DeleteToken.objects.filter(email="test@example.com").exists()

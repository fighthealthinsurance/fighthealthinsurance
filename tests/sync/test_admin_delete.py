"""Tests for admin delete user data view."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class TestAdminDeleteDataView(TestCase):
    """Test the staff-only admin delete data view."""

    def setUp(self):
        self.url = reverse("admin_delete_user_data")
        self.staff_user = User.objects.create_user(
            username="staffuser",
            password="testpass123",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="regularuser",
            password="testpass123",
            is_staff=False,
        )

    def test_requires_staff_login(self):
        """Anonymous users should be redirected to login."""
        response = self.client.get(self.url)
        assert response.status_code == 302
        assert "/admin/login/" in response.url or "login" in response.url

    def test_non_staff_get_redirected(self):
        """Non-staff users should be redirected on GET."""
        self.client.login(username="regularuser", password="testpass123")
        response = self.client.get(self.url)
        assert response.status_code == 302

    def test_non_staff_post_redirected(self):
        """Non-staff users should be redirected on POST (destructive endpoint)."""
        self.client.login(username="regularuser", password="testpass123")
        response = self.client.post(self.url, {"email": "user@example.com"})
        assert response.status_code == 302

    def test_staff_can_access_get(self):
        """Staff users should see the delete form."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert b"Delete User Data" in response.content

    @patch(
        "fighthealthinsurance.staff_views.RemoveDataHelper.remove_data_for_email"
    )
    def test_staff_can_delete_data(self, mock_remove):
        """Staff POST with valid email should call RemoveDataHelper."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(self.url, {"email": "user@example.com"})
        assert response.status_code == 200
        mock_remove.assert_called_once_with("user@example.com")
        assert b"has been deleted" in response.content

    def test_post_invalid_email_rejected(self):
        """POST with invalid email should show form errors."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(self.url, {"email": "not-an-email"})
        assert response.status_code == 200
        # Should re-render the form (not a success message)
        assert b"has been deleted" not in response.content

    @patch(
        "fighthealthinsurance.staff_views.RemoveDataHelper.remove_data_for_email",
        side_effect=Exception("DB error"),
    )
    def test_deletion_error_returns_500(self, mock_remove):
        """When deletion raises an exception, should return 500 error."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(self.url, {"email": "user@example.com"})
        assert response.status_code == 500
        assert b"Error deleting data" in response.content
        assert b"has been deleted" not in response.content

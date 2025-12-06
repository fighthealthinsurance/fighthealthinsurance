"""Tests for the staff dashboard."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


User = get_user_model()


class TestStaffDashboard(TestCase):
    """Tests for staff dashboard access and functionality."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        """Create test users."""
        self.staff_user = User.objects.create_user(
            username="staffuser",
            email="staff@test.com",
            password="testpass123",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="regularuser",
            email="regular@test.com",
            password="testpass123",
            is_staff=False,
        )

    def test_staff_dashboard_requires_staff(self):
        """Test that non-staff users are redirected from staff dashboard."""
        self.client.login(username="regularuser", password="testpass123")
        response = self.client.get(reverse("staff_dashboard"))
        # Should redirect to admin login
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_staff_dashboard_accessible_by_staff(self):
        """Test that staff users can access the staff dashboard."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Staff Dashboard")

    def test_staff_dashboard_contains_expected_sections(self):
        """Test that the staff dashboard contains all expected sections."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get(reverse("staff_dashboard"))

        # Check for main sections
        self.assertContains(response, "Email")
        self.assertContains(response, "Communication")
        self.assertContains(response, "User Management")
        self.assertContains(response, "Scheduling")
        self.assertContains(response, "Charts")
        self.assertContains(response, "Analytics")
        self.assertContains(response, "Data Exports")
        self.assertContains(response, "Django Admin")

    def test_staff_dashboard_contains_key_links(self):
        """Test that the staff dashboard contains key navigation links."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get(reverse("staff_dashboard"))

        # Check for key links
        self.assertContains(response, "Send Mailing List Email")
        self.assertContains(response, "Activate Pro User")
        self.assertContains(response, "Enable Beta Features")
        self.assertContains(response, "Schedule Follow-ups")
        self.assertContains(response, "Signups by Day")
        self.assertContains(response, "De-Identified Data Export")
        self.assertContains(response, "Open Django Admin")

    def test_anonymous_user_redirected(self):
        """Test that anonymous users are redirected to login."""
        response = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

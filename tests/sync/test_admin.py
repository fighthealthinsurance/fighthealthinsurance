"""Tests for Django admin configuration."""

from django.test import TestCase, Client
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model

from fighthealthinsurance.admin import (
    ChatLeadsAdmin,
    DenialAdmin,
    AppealAdmin,
    OngoingChatAdmin,
)
from fighthealthinsurance.models import (
    ChatLeads,
    Denial,
    Appeal,
    OngoingChat,
)

User = get_user_model()


class TestAdminRegistration(TestCase):
    """Test that admin classes are properly registered."""

    def test_admin_classes_exist(self):
        """Admin classes should be importable."""
        self.assertIsNotNone(ChatLeadsAdmin)
        self.assertIsNotNone(DenialAdmin)
        self.assertIsNotNone(AppealAdmin)
        self.assertIsNotNone(OngoingChatAdmin)


class TestChatLeadsAdmin(TestCase):
    """Test ChatLeadsAdmin configuration."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = ChatLeadsAdmin(ChatLeads, self.site)

    def test_list_display(self):
        """Should have proper list_display fields."""
        self.assertIn("name", self.admin.list_display)
        self.assertIn("email", self.admin.list_display)
        self.assertIn("company", self.admin.list_display)

    def test_search_fields(self):
        """Should have searchable fields."""
        self.assertIn("company", self.admin.search_fields)
        self.assertIn("name", self.admin.search_fields)


class TestDenialAdmin(TestCase):
    """Test DenialAdmin configuration."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = DenialAdmin(Denial, self.site)

    def test_list_display(self):
        """Should have proper list_display fields."""
        self.assertTrue(len(self.admin.list_display) > 0)

    def test_search_fields(self):
        """Should have searchable fields."""
        self.assertTrue(len(self.admin.search_fields) > 0)


class TestAdminAccess(TestCase):
    """Test admin page access for superusers."""

    # Note: Admin is mounted at /timbit/admin/ in this project
    ADMIN_URL = "/timbit/admin/"

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )

    def test_admin_login_page(self):
        """Admin login page should be accessible."""
        response = self.client.get(f"{self.ADMIN_URL}login/")
        self.assertEqual(response.status_code, 200)

    def test_admin_index_requires_login(self):
        """Admin index should redirect to login."""
        response = self.client.get(self.ADMIN_URL)
        self.assertEqual(response.status_code, 302)  # Redirect to login

    def test_admin_index_accessible_when_logged_in(self):
        """Admin index should be accessible to logged in superuser."""
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(self.ADMIN_URL)
        self.assertEqual(response.status_code, 200)

    def test_denial_changelist(self):
        """Denial changelist should be accessible."""
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(f"{self.ADMIN_URL}fighthealthinsurance/denial/")
        self.assertEqual(response.status_code, 200)

    def test_appeal_changelist(self):
        """Appeal changelist should be accessible."""
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(f"{self.ADMIN_URL}fighthealthinsurance/appeal/")
        self.assertEqual(response.status_code, 200)

    def test_chatleads_changelist(self):
        """ChatLeads changelist should be accessible."""
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(f"{self.ADMIN_URL}fighthealthinsurance/chatleads/")
        self.assertEqual(response.status_code, 200)


class TestAdminActions(TestCase):
    """Test admin custom actions work."""

    ADMIN_URL = "/timbit/admin/"

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )
        self.client.login(username="admin", password="adminpass123")

    def test_denial_add_form(self):
        """Denial add form should load."""
        response = self.client.get(f"{self.ADMIN_URL}fighthealthinsurance/denial/add/")
        self.assertEqual(response.status_code, 200)

    def test_appeal_add_form(self):
        """Appeal add form should load."""
        response = self.client.get(f"{self.ADMIN_URL}fighthealthinsurance/appeal/add/")
        self.assertEqual(response.status_code, 200)

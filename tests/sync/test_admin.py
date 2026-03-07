"""Tests for Django admin configuration."""

import json

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


class TestOngoingChatAdmin(TestCase):
    """Test OngoingChatAdmin configuration."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = OngoingChatAdmin(OngoingChat, self.site)

    def test_change_form_template_set(self):
        """Should use custom chat editor template."""
        self.assertIn("chat_editor", self.admin.change_form_template)

    def test_list_display_includes_message_count(self):
        """Should show message count in list."""
        self.assertIn("message_count", self.admin.list_display)

    def test_list_display_includes_has_edited(self):
        """Should show edited status in list."""
        self.assertIn("has_edited", self.admin.list_display)

    def test_search_fields_configured(self):
        """Should have searchable fields."""
        self.assertIn("denied_item", self.admin.search_fields)
        self.assertIn("session_key", self.admin.search_fields)

    def test_readonly_fields_includes_chat_history(self):
        """Original chat_history should be read-only."""
        self.assertIn("chat_history", self.admin.readonly_fields)

    def test_fieldsets_structured(self):
        """Should have Chat Data and Denial Info fieldsets."""
        fieldset_names = [fs[0] for fs in self.admin.fieldsets]
        self.assertIn("Chat Data", fieldset_names)
        self.assertIn("Denial Info", fieldset_names)

    def test_message_count_empty(self):
        """Should return 0 for empty chat history."""
        chat = OngoingChat()
        self.assertEqual(self.admin.message_count(chat), 0)

    def test_message_count_with_messages(self):
        """Should return correct count for messages."""
        chat = OngoingChat(
            chat_history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        )
        self.assertEqual(self.admin.message_count(chat), 2)

    def test_has_edited_false_when_empty(self):
        """Should return False when no edited history."""
        chat = OngoingChat()
        self.assertFalse(self.admin.has_edited(chat))

    def test_has_edited_false_when_none(self):
        """Should return False when edited history is explicitly None."""
        chat = OngoingChat(edited_chat_history=None)
        self.assertFalse(self.admin.has_edited(chat))

    def test_has_edited_true_when_present(self):
        """Should return True when edited history exists."""
        chat = OngoingChat(edited_chat_history=[{"role": "user", "content": "edited"}])
        self.assertTrue(self.admin.has_edited(chat))


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

    def test_ongoingchat_changelist(self):
        """OngoingChat changelist should be accessible."""
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/")
        self.assertEqual(response.status_code, 200)

    def test_ongoingchat_change_form_with_custom_template(self):
        """OngoingChat change form should load with custom chat editor template."""
        self.client.login(username="admin", password="adminpass123")
        chat = OngoingChat.objects.create(
            chat_history=[{"role": "user", "content": "test message"}]
        )
        response = self.client.get(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/{chat.pk}/change/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "chat-editor-container")


class TestOngoingChatAdminEditing(TestCase):
    """Test editing OngoingChat via admin form."""

    ADMIN_URL = "/timbit/admin/"

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )
        self.client.login(username="admin", password="adminpass123")

    def test_save_edited_chat_history_persists(self):
        """Edited chat history should persist when saved via admin."""
        chat = OngoingChat.objects.create(
            chat_history=[{"role": "user", "content": "original msg"}],
        )
        edited = [{"role": "user", "content": "cleaned up msg"}]
        response = self.client.post(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/{chat.pk}/change/",
            {
                "is_patient": "on",
                "edited_chat_history": json.dumps(edited),
                "denied_item": "",
                "denied_reason": "",
                "session_key": "",
                "hashed_email": "",
                "microsite_slug": "",
                "user_agent": "",
                "asn": "",
                "asn_name": "",
                "_save": "Save",
            },
        )
        # Should redirect on success (302) to changelist
        self.assertEqual(response.status_code, 302)
        chat.refresh_from_db()
        self.assertEqual(len(chat.edited_chat_history), 1)
        self.assertEqual(chat.edited_chat_history[0]["content"], "cleaned up msg")


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

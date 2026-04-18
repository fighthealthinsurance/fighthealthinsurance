"""Tests for Django admin configuration."""

import json

from django.test import TestCase, Client, RequestFactory
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

    def test_message_count_with_scalar_returns_zero(self):
        """Should return 0 when chat_history is a scalar (malformed data)."""
        chat = OngoingChat(chat_history=42)
        self.assertEqual(self.admin.message_count(chat), 0)

    def test_message_count_with_dict_returns_zero(self):
        """Should return 0 when chat_history is a dict (malformed data)."""
        chat = OngoingChat(chat_history={"key": "val"})
        self.assertEqual(self.admin.message_count(chat), 0)

    def test_has_edited_false_when_empty(self):
        """Should return False when no edited history."""
        chat = OngoingChat()
        self.assertFalse(self.admin.has_edited(chat))

    def test_has_edited_false_when_none(self):
        """Should return False when edited history is explicitly None."""
        chat = OngoingChat(edited_chat_history=None)
        self.assertFalse(self.admin.has_edited(chat))

    def test_has_edited_false_with_scalar(self):
        """Should return False when edited_chat_history is a scalar (malformed data)."""
        chat = OngoingChat(edited_chat_history="not a list")
        self.assertFalse(self.admin.has_edited(chat))

    def test_has_edited_true_when_present(self):
        """Should return True when edited history exists."""
        chat = OngoingChat(edited_chat_history=[{"role": "user", "content": "edited"}])
        self.assertTrue(self.admin.has_edited(chat))

    def test_message_count_has_admin_order_field(self):
        """message_count should be sortable via chat_message_count annotation."""
        self.assertEqual(
            OngoingChatAdmin.message_count.admin_order_field, "chat_message_count"
        )

    def test_message_count_prefers_annotation(self):
        """message_count should use the annotated value when present."""
        chat = OngoingChat(chat_history=[{"role": "user", "content": "hi"}])
        chat.chat_message_count = 42  # Simulated annotation
        self.assertEqual(self.admin.message_count(chat), 42)


class TestOngoingChatAdminQueryset(TestCase):
    """Test OngoingChatAdmin.get_queryset annotation for message count sorting."""

    def setUp(self):
        self.site = AdminSite()
        self.admin = OngoingChatAdmin(OngoingChat, self.site)
        self.factory = RequestFactory()
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )

    def _request(self):
        request = self.factory.get("/")
        request.user = self.admin_user
        return request

    def test_get_queryset_annotates_message_count_for_list(self):
        """get_queryset should expose chat_message_count matching list length."""
        OngoingChat.objects.create(chat_history=[])
        OngoingChat.objects.create(chat_history=[{"role": "user", "content": "a"}])
        OngoingChat.objects.create(
            chat_history=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
            ]
        )
        qs = self.admin.get_queryset(self._request())
        counts = sorted(chat.chat_message_count for chat in qs)
        self.assertEqual(counts, [0, 1, 3])

    def test_get_queryset_handles_non_array_chat_history(self):
        """get_queryset should return 0 for non-array JSON (malformed data)."""
        # Default empty list
        OngoingChat.objects.create(chat_history=[])
        # Dict (not an array) - should count as 0
        OngoingChat.objects.create(chat_history={"not": "an array"})
        # Scalar - should count as 0
        OngoingChat.objects.create(chat_history=42)
        qs = self.admin.get_queryset(self._request())
        counts = sorted(chat.chat_message_count for chat in qs)
        self.assertEqual(counts, [0, 0, 0])

    def test_get_queryset_enables_ordering_by_message_count(self):
        """Annotation should allow ORDER BY chat_message_count."""
        small = OngoingChat.objects.create(
            chat_history=[{"role": "user", "content": "hi"}]
        )
        big = OngoingChat.objects.create(
            chat_history=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]
        )
        qs = self.admin.get_queryset(self._request()).order_by("chat_message_count")
        ordered_ids = list(qs.values_list("id", flat=True))
        self.assertEqual(ordered_ids, [small.id, big.id])
        # Reverse ordering
        qs_desc = self.admin.get_queryset(self._request()).order_by(
            "-chat_message_count"
        )
        self.assertEqual(list(qs_desc.values_list("id", flat=True)), [big.id, small.id])


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
        # Editor-specific elements rendered only inside {% if original %}
        self.assertContains(response, "Copy original to edited")
        self.assertContains(response, 'id="original-messages"')
        self.assertContains(response, 'id="edited-messages"')
        # chat_history injected via json_script (not read from readonly widget)
        self.assertContains(response, 'id="chat-history-data"')
        self.assertContains(response, "test message")

    def test_ongoingchat_add_form_shows_raw_fields(self):
        """OngoingChat add form should show raw JSON fields, not the editor."""
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/add/"
        )
        self.assertEqual(response.status_code, 200)
        # Editor should NOT be rendered on add page
        self.assertNotContains(response, 'id="original-messages"')
        # The CSS hiding rule should NOT be emitted on the add page
        self.assertNotContains(response, ".field-chat_history,")

    def test_ongoingchat_change_form_has_copy_toolbar(self):
        """Change form should include copy toolbar buttons for both tabs."""
        self.client.login(username="admin", password="adminpass123")
        chat = OngoingChat.objects.create(
            chat_history=[{"role": "user", "content": "test"}]
        )
        response = self.client.get(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/{chat.pk}/change/"
        )
        self.assertEqual(response.status_code, 200)
        # Both tabs should have toolbars
        self.assertContains(response, 'id="original-copy-toolbar"')
        self.assertContains(response, 'id="edited-copy-toolbar"')
        # Copy buttons should be present
        self.assertContains(response, "Copy Selected as Text")
        self.assertContains(response, "Copy Selected as JSON")
        self.assertContains(response, "Copy All as Text")
        self.assertContains(response, "Copy All as JSON")
        self.assertContains(response, "Select All")
        self.assertContains(response, "Deselect All")

    def test_ongoingchat_change_form_has_accessible_toast(self):
        """Change form should include an accessible live region for copy feedback."""
        self.client.login(username="admin", password="adminpass123")
        chat = OngoingChat.objects.create(chat_history=[])
        response = self.client.get(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/{chat.pk}/change/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="copy-toast"')
        self.assertContains(response, 'role="status"')
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'aria-atomic="true"')

    def test_ongoingchat_changelist_sortable_by_message_count(self):
        """Changelist should load when sorted by the message_count column."""
        self.client.login(username="admin", password="adminpass123")
        OngoingChat.objects.create(chat_history=[{"role": "user", "content": "a"}])
        OngoingChat.objects.create(
            chat_history=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]
        )
        # Django admin sort uses 1-indexed list_display position; message_count is
        # the 8th entry in list_display. Verify the URL works without SQL errors.
        response = self.client.get(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/?o=8"
        )
        self.assertEqual(response.status_code, 200)
        # Descending sort
        response = self.client.get(
            f"{self.ADMIN_URL}fighthealthinsurance/ongoingchat/?o=-8"
        )
        self.assertEqual(response.status_code, 200)


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

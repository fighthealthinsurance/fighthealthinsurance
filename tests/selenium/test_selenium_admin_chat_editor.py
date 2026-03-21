"""Selenium tests for admin chat editor UI.

Tests verify that:
1. The Original Chat tab displays messages from chat_history (via json_script)
2. The copy-to-edited workflow persists edited_chat_history to the database
"""

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from fighthealthinsurance.models import OngoingChat

from .fhi_selenium_base import FHISeleniumBase

User = get_user_model()
BaseCase.main(__name__, __file__)

ADMIN_URL = "/timbit/admin/"


class SeleniumTestAdminChatEditor(FHISeleniumBase, StaticLiveServerTestCase):
    """Test admin chat editor UI with Selenium."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )
        self.chat = OngoingChat.objects.create(
            chat_history=[
                {
                    "role": "user",
                    "content": "I was denied coverage",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "I can help with that",
                    "timestamp": "2026-01-01T00:01:00Z",
                },
            ]
        )

    def _admin_login(self):
        """Log into Django admin via the login form."""
        self.open(f"{self.live_server_url}{ADMIN_URL}login/")
        self.type("#id_username", "admin")
        self.type("#id_password", "adminpass123")
        self.click("input[type='submit']")
        # Wait for #site-name which only appears on authenticated admin pages
        self.wait_for_element("#site-name", timeout=10)

    def _open_chat_change_page(self):
        """Navigate to the OngoingChat change page."""
        self.open(
            f"{self.live_server_url}{ADMIN_URL}"
            f"fighthealthinsurance/ongoingchat/{self.chat.pk}/change/"
        )
        self.wait_for_element(".chat-editor-container", timeout=10)

    def test_original_tab_displays_messages(self):
        """Original Chat tab should display chat messages from json_script data."""
        self._admin_login()
        self._open_chat_change_page()
        # Original tab is active by default
        self.assert_text("I was denied coverage", "#original-messages")
        self.assert_text("I can help with that", "#original-messages")

    def test_copy_edit_save_persists(self):
        """Copy original to edited, save, and verify persistence in DB."""
        self._admin_login()
        self._open_chat_change_page()

        # Switch to Edited tab
        self.click("button[data-tab='edited']")
        # Click copy button
        self.click(".copy-to-edited-btn")
        # Verify edited messages appeared
        self.wait_for_element("#edited-messages .chat-message", timeout=5)
        self.assert_text("I was denied coverage", "#edited-messages")

        # Save the form
        self.click("input[name='_save']")
        # Should redirect to changelist (wait for changelist-specific element)
        self.wait_for_element("#result_list", timeout=10)

        # Verify DB persistence
        self.chat.refresh_from_db()
        self.assertIsNotNone(self.chat.edited_chat_history)
        self.assertEqual(len(self.chat.edited_chat_history), 2)
        self.assertEqual(
            self.chat.edited_chat_history[0]["content"], "I was denied coverage"
        )
        self.assertEqual(
            self.chat.edited_chat_history[1]["content"], "I can help with that"
        )

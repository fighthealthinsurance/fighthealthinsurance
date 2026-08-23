"""External AI models must be opt-out: the consent form ships with the box checked.

These cover the Django half only — the checkbox state the consent pages render
before any stored preference is applied. The client-side half (localStorage
resolution in ``user_info_storage.ts``, which is what actually reaches the chat)
is covered by ``tests/selenium/test_selenium_chat_status.py``.
"""

import re

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.chat_forms import UserConsentForm

# The rendered checkbox tag, whatever order Django emits its attributes in.
EXTERNAL_MODELS_INPUT = re.compile(r'<input[^>]*id="use_external_models"[^>]*>')


class ChatConsentExternalModelsDefaultTest(TestCase):
    """The rendered default is what a user sees before any prior choice applies."""

    def setUp(self):
        self.client = Client()

    def assertRendersCheckboxChecked(self, response):
        self.assertEqual(response.status_code, 200)
        match = EXTERNAL_MODELS_INPUT.search(response.content.decode())
        self.assertIsNotNone(match, "use_external_models checkbox missing from page")
        self.assertIn("checked", match.group(0))

    def test_form_field_defaults_to_enabled(self):
        form = UserConsentForm()
        self.assertTrue(form.fields["use_external_models"].initial)

    def test_consent_page_renders_checkbox_checked(self):
        self.assertRendersCheckboxChecked(self.client.get(reverse("chat_consent")))

    def test_explain_denial_page_renders_checkbox_checked(self):
        """The other page sharing the consent partial gets the same default."""
        self.assertRendersCheckboxChecked(self.client.get(reverse("explain_denial")))

    def test_unchecked_submission_is_still_respected(self):
        """Default-on must not be hardened into force-on (required, or clean()ed True)."""
        form = UserConsentForm(
            data={
                "first_name": "Test",
                "last_name": "User",
                "email": "external-default@example.com",
                "tos_agreement": "on",
                "privacy_policy": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["use_external_models"])

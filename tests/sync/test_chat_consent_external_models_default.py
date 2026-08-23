"""External AI models must be opt-out: the consent form ships with the box checked."""

import re

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.chat_forms import UserConsentForm

# The rendered checkbox tag, whatever order Django emits its attributes in.
EXTERNAL_MODELS_INPUT = re.compile(r'<input[^>]*id="use_external_models"[^>]*>')


def rendered_external_models_input(html: str) -> str:
    match = EXTERNAL_MODELS_INPUT.search(html)
    assert match is not None, "use_external_models checkbox missing from page"
    return match.group(0)


class ChatConsentExternalModelsDefaultTest(TestCase):
    """The server-rendered default is the one users get without any JavaScript."""

    def setUp(self):
        self.client = Client()

    def test_form_field_defaults_to_enabled(self):
        form = UserConsentForm()
        self.assertTrue(form.fields["use_external_models"].initial)

    def test_consent_page_renders_checkbox_checked(self):
        response = self.client.get(reverse("chat_consent"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "checked",
            rendered_external_models_input(response.content.decode()),
        )

    def test_explain_denial_page_renders_checkbox_checked(self):
        """The other page sharing the consent partial gets the same default."""
        response = self.client.get(reverse("explain_denial"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "checked",
            rendered_external_models_input(response.content.decode()),
        )

    def test_unchecked_submission_is_still_respected(self):
        """Default-on must not become force-on."""
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

"""External AI models must be opt-out: the consent form ships with the box checked.

These cover the Django half — the checkbox state the consent pages render. The
client-side half (localStorage resolution in ``user_info_storage.ts``) is covered
by ``tests/selenium/test_selenium_chat_status.py``.
"""

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.chat_forms import UserConsentForm

CHECKED_CHECKBOX = (
    '<input type="checkbox" name="use_external_models" '
    'class="form-check-input" id="use_external_models" checked>'
)


class ChatConsentExternalModelsDefaultTest(TestCase):
    """The rendered default is what a user sees before any prior choice applies."""

    def assertRendersCheckboxChecked(self, response):
        # Exact widget markup, not a substring search for "checked": a tag
        # carrying value="checked" but no boolean checked attribute (the style
        # used in scrub.html) must not satisfy this. html=True would be the
        # house idiom, but it parses the whole response and both pages carry a
        # pre-existing unbalanced </div> that trips the parser.
        self.assertContains(response, CHECKED_CHECKBOX)

    def test_form_renders_field_enabled(self):
        # value(), not fields[...].initial: a view's get_initial() overrides the
        # field default, and value() is what actually reaches the template.
        self.assertTrue(UserConsentForm()["use_external_models"].value())

    def test_consent_page_renders_checkbox_checked(self):
        self.assertRendersCheckboxChecked(self.client.get(reverse("chat_consent")))

    def test_explain_denial_page_renders_checkbox_checked(self):
        """The other page sharing the consent partial gets the same default."""
        self.assertRendersCheckboxChecked(self.client.get(reverse("explain_denial")))

    def test_unchecked_submission_cleans_to_false(self):
        """Default-on must not be hardened into force-on (required, or clean()ed True).

        The consent POST does not carry the preference to the backend — that
        travels via localStorage — so this guards the form, not the request.
        """
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

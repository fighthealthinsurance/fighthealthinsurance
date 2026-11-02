"""The chat page's consent gate (views.chat_interface_view and
ChatUserConsentView): no chat page until both agreements are ticked, and a
refused form keeps nothing in the session."""

from django.test import TestCase
from django.urls import reverse

# Obviously fake details, named for Elizabeth Blackwell, the first woman to
# earn a medical degree in the United States.
FORM = {
    "first_name": "Elizabeth",
    "last_name": "Blackwell",
    "email": "elizabeth.blackwell@example.com",
    "tos_agreement": "on",
    "privacy_policy": "on",
}


class ChatConsentGateTest(TestCase):
    def setUp(self):
        self.chat = reverse("chat")
        self.consent = reverse("chat_consent")

    def test_the_chat_page_sends_a_new_browser_to_the_consent_form(self):
        response = self.client.get(self.chat)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(self.consent))

    def test_the_redirect_carries_the_treatment_the_guide_sent(self):
        response = self.client.get(self.chat, {"default_procedure": "MRI"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("default_procedure=MRI", response["Location"])

    def test_the_redirect_never_carries_a_denial_letter_in_the_address(self):
        response = self.client.get(
            self.chat, {"denial_text": "Elizabeth Blackwell was denied an MRI"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Blackwell", response["Location"])

    def _refused(self, **changes):
        data = {**FORM, **changes}
        data = {k: v for k, v in data.items() if v is not None}
        response = self.client.post(self.consent, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "chat_consent.html")
        self.assertTrue(response.context["form"].errors)
        self.assertNotIn("consent_completed", self.client.session)
        self.assertNotIn("email", self.client.session)
        follow = self.client.get(self.chat)
        self.assertEqual(follow.status_code, 302)
        return response

    def test_without_the_terms_ticked_there_is_no_chat(self):
        response = self._refused(tos_agreement=None)
        self.assertIn("tos_agreement", response.context["form"].errors)

    def test_without_the_privacy_policy_ticked_there_is_no_chat(self):
        response = self._refused(privacy_policy=None)
        self.assertIn("privacy_policy", response.context["form"].errors)

    def test_without_an_email_there_is_no_chat(self):
        response = self._refused(email=None)
        self.assertIn("email", response.context["form"].errors)

    def test_with_a_malformed_email_there_is_no_chat(self):
        response = self._refused(email="not-an-email")
        self.assertIn("email", response.context["form"].errors)

    def test_with_both_ticked_the_chat_page_opens(self):
        response = self.client.post(self.consent, FORM)
        self.assertRedirects(response, "/chat/", fetch_redirect_response=False)
        page = self.client.get(self.chat)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "chat_interface.html")

    def test_consent_in_one_browser_does_not_open_the_chat_in_another(self):
        self.client.post(self.consent, FORM)
        other = self.client_class()
        self.assertEqual(other.get(self.chat).status_code, 302)

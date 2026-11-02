"""Each field on the intake and consent forms says what it needs, out loud.

Oct 2026 accessibility audit, items M5, M6, M9, M10 and M11:

- The hints under a field (what we keep, where it goes) were only next to
  it on screen; a screen reader reached the field without them. Each hint
  now has an id in its field's aria-describedby, and a message the server
  or the page's script adds joins those ids rather than replacing them.
- The letter and the email on /scan and the assistant terms page are
  required and were not marked so.
- Explain My Denial, Understand My Policy and the chat's terms page printed
  the form's internal names ("first_name: This field is required."). They
  now say under each field what to fill in, list it at the top with a link
  to the field, and put "Error:" first in the page title.
- The draft on the send page had no label, and its fax price radios were a
  div with an aria-label, which nothing announces.
- The chat's message box had only a placeholder, its messages were not a
  live region, and "typing" was not announced.
"""

import pathlib

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.chat_forms import UnderstandPolicyForm, UserConsentForm
from tests.sync.test_the_send_page_keeps_your_letter import SendPageTestCase

CHAT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
    / "chat_interface.tsx"
)


def soup_of(response) -> BeautifulSoup:
    assert response.status_code == 200, response.status_code
    return BeautifulSoup(response.content.decode(), "html.parser")


def described_by(soup: BeautifulSoup, field_id: str) -> list[str]:
    field = soup.find(id=field_id)
    assert field is not None, field_id
    return (field.get("aria-describedby") or "").split()


class HintsAreTiedToTheirFieldsMixin:
    """Every id a field is described by is on the page."""

    def assert_described_by_hints(self, soup, field_id, hint_ids):
        ids = described_by(soup, field_id)
        for hint_id in hint_ids:
            self.assertIn(hint_id, ids, field_id)
            self.assertIsNotNone(soup.find(id=hint_id), hint_id)


class IntakePageTest(HintsAreTiedToTheirFieldsMixin, TestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.soup = soup_of(Client().get(reverse("scan")))

    def test_the_letter_is_described_by_both_its_hints(self):
        self.assert_described_by_hints(
            self.soup, "denial_text", ["denial_text_hint", "denial_text_sent_hint"]
        )

    def test_the_letter_and_the_email_are_marked_required(self):
        for field_id, label_id in (
            ("denial_text", "denial_text_label"),
            ("email", "email-label"),
        ):
            with self.subTest(field=field_id):
                field = self.soup.find(id=field_id)
                self.assertEqual(field.get("aria-required"), "true")
                self.assertIn("(required)", self.soup.find(id=label_id).get_text())

    def test_the_remove_details_button_and_referral_details_have_their_hints(self):
        self.assert_described_by_hints(self.soup, "scrub-2", ["scrub_hint"])
        self.assert_described_by_hints(
            self.soup, "referral_source_details", ["referral_source_details_hint"]
        )

    def test_a_refused_letter_keeps_its_hints_and_adds_its_message(self):
        soup = soup_of(Client().post(reverse("process"), {}))
        self.assertEqual(
            described_by(soup, "denial_text"),
            ["denial_text_hint", "denial_text_sent_hint", "need_denial"],
        )


class AssistantTermsPageTest(HintsAreTiedToTheirFieldsMixin, TestCase):
    def setUp(self):
        self.soup = BeautifulSoup(
            render_to_string(
                "assistant_terms.html",
                {
                    "form": core_forms.AssistantTermsForm(),
                    "on_behalf_choices": core_forms.AssistantTermsForm.ON_BEHALF_CHOICES,
                    "token": "a-token",
                },
            ),
            "html.parser",
        )

    def test_the_letter_and_the_email_are_marked_required(self):
        label = self.soup.find("label", attrs={"for": "denial_text"})
        self.assertIn("(required)", label.get_text())
        self.assertIn("(required)", self.soup.find(id="email-label").get_text())
        for field_id in ("denial_text", "email"):
            with self.subTest(field=field_id):
                self.assertEqual(self.soup.find(id=field_id).get("aria-required"), "true")

    def test_the_hints_are_tied_to_their_fields(self):
        self.assert_described_by_hints(
            self.soup, "denial_text", ["denial_text_hint", "denial_text_sent_hint"]
        )
        self.assert_described_by_hints(
            self.soup, "referral_source_details", ["referral_source_details_hint"]
        )


class ConsentPagesSayWhatToFillInTest(HintsAreTiedToTheirFieldsMixin, TestCase):
    """The three pages that use the consent form fields."""

    PAGES = ("chat_consent", "explain_denial", "understand_policy")

    def refused(self, page):
        return soup_of(Client().post(reverse(page), {}))

    def test_a_refused_form_names_no_internal_field(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                text = self.refused(page).get_text(" ")
                for name in ("first_name:", "last_name:", "tos_agreement:", "__all__"):
                    self.assertNotIn(name, text)
                self.assertNotIn("This field is required", text)

    def test_the_title_says_error_first(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                title = self.refused(page).title.get_text(strip=True)
                self.assertTrue(title.startswith("Error:"), title)
                fresh = soup_of(Client().get(reverse(page))).title.get_text(strip=True)
                self.assertFalse(fresh.startswith("Error:"), fresh)

    def test_the_summary_links_each_message_to_its_field(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                soup = self.refused(page)
                summary = soup.find(class_="fhi-form-errors")
                self.assertEqual(summary.get("role"), "alert")
                links = {a["href"]: a.get_text() for a in summary.find_all("a")}
                self.assertEqual(links.get("#store_fname"), "Please enter your first name.")
                for href in links:
                    self.assertIsNotNone(soup.find(id=href[1:]), href)

    def test_each_message_sits_under_its_field_and_describes_it(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                soup = self.refused(page)
                message = soup.find(id="id_first_name_error")
                self.assertEqual(message.get_text(), "Please enter your first name.")
                self.assertIn("id_first_name_error", described_by(soup, "store_fname"))
                self.assertEqual(soup.find(id="store_fname").get("aria-invalid"), "true")
                self.assertEqual(
                    soup.find(id="id_tos_agreement_error").get_text(),
                    "Please tick the box to agree to the Terms of Service.",
                )

    def test_an_email_refused_for_its_shape_says_why_and_keeps_its_hint(self):
        soup = soup_of(Client().post(reverse("chat_consent"), {"email": "someone@"}))
        self.assertIn("valid email", soup.find(id="id_email_error").get_text())
        self.assertEqual(described_by(soup, "email"), ["email_hint", "id_email_error"])

    def test_a_fresh_page_shows_no_message(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                soup = soup_of(Client().get(reverse(page)))
                self.assertIsNone(soup.find(class_="fhi-form-errors"))
                self.assertEqual(soup.find(id="id_first_name_error").get_text(), "")
                self.assertNotIn("id_first_name_error", described_by(soup, "store_fname"))

    def test_the_consent_fields_hints_are_tied_to_their_fields(self):
        for page in ("chat_consent", "explain_denial"):
            soup = soup_of(Client().get(reverse(page)))
            for name, hint_ids in UserConsentForm.FIELD_HINTS.items():
                field_id = UserConsentForm()[name].id_for_label
                with self.subTest(page=page, field=field_id):
                    self.assert_described_by_hints(soup, field_id, hint_ids)

    def test_the_explain_denial_letter_has_its_tip(self):
        soup = soup_of(Client().get(reverse("explain_denial")))
        self.assert_described_by_hints(soup, "denial_text", ["denial_text_tip"])

    def test_the_policy_fields_hints_are_tied_to_their_fields(self):
        soup = soup_of(Client().get(reverse("understand_policy")))
        for name, hint_ids in UnderstandPolicyForm.FIELD_HINTS.items():
            field_id = UnderstandPolicyForm()[name].id_for_label
            with self.subTest(field=field_id):
                self.assert_described_by_hints(soup, field_id, hint_ids)

    def test_a_missing_policy_document_says_what_to_do(self):
        soup = self.refused("understand_policy")
        self.assertEqual(
            soup.find(id="id_policy_document_error").get_text(),
            "Please choose your policy document to upload.",
        )
        self.assertEqual(
            described_by(soup, "policy_document"),
            ["policy_document_hint", "policy_document_training_hint", "id_policy_document_error"],
        )


class SendPageLabelsTest(SendPageTestCase):
    def test_the_draft_has_a_label(self):
        soup = self.render()
        label = soup.find("label", attrs={"for": "scrubbed_appeal_text"})
        self.assertIsNotNone(label)
        self.assertEqual(label.get_text(strip=True), "Your appeal letter (you can edit it)")

    def test_the_fax_price_radios_are_a_named_group(self):
        soup = self.render()
        radio = soup.find("input", attrs={"name": "fax_pwyw"})
        group = radio.find_parent("fieldset")
        self.assertIsNotNone(group, "the fax price radios are not in a fieldset")
        self.assertIn("fax-pwyw-group", group.get("class"))
        self.assertIsNone(group.get("aria-label"))
        self.assertEqual(
            group.find("legend").get_text(strip=True),
            "Fax price (optional, pay what you want)",
        )
        self.assertEqual(len(group.find_all("input", attrs={"name": "fax_pwyw"})), 4)


class ChatIsAnnouncedTest(TestCase):
    """There is no harness that runs the React chat, so this reads the source,
    like test_chat_upload_kept_until_sent.py."""

    def setUp(self):
        self.src = CHAT.read_text()

    def test_both_message_boxes_have_a_name(self):
        self.assertEqual(self.src.count('aria-label="Message to the assistant"'), 2)

    def test_the_messages_are_a_polite_log(self):
        self.assertIn('role="log"', self.src)
        log = self.src[self.src.index('role="log"') :][:200]
        self.assertIn('aria-live="polite"', log)
        self.assertIn("state.messages.map(renderMessage)", self.src[self.src.index('role="log"') :][:400])

    def test_typing_is_announced_once_not_with_every_dot(self):
        start = self.src.index("const TypingAnimation")
        body = self.src[start : self.src.index("\n};\n", start)]
        self.assertIn('role="status"', body)
        self.assertIn("Assistant is typing", body)
        self.assertIn('aria-hidden="true"', body)

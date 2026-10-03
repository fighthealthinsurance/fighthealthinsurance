"""The chat consent says what happens to each detail it asks for, and the
server and the browser do what it says.

The details form is one partial (partials/user_consent_form_fields.html)
shared by the chat consent page and Explain My Denial, and both views handle
it the same way, so every check runs on both pages. Each test pins the
sentence the page shows about one detail against the code that carries it
out: the views for what the server keeps, and user_info_storage.ts for what
the browser keeps and what the chat takes out of a message before sending it.

What the server keeps is read from everywhere it could keep it: every table,
with each saved session decoded. A detail the page says the server does not
keep must appear in none of them.
"""

import pathlib
import re
from html.parser import HTMLParser
from importlib import import_module

from asgiref.sync import async_to_sync
from django.apps import apps
from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.chat_forms import UserConsentForm
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import (
    ChatType,
    Denial,
    MailingListSubscriber,
    OngoingChat,
)
from fighthealthinsurance.websockets import OngoingChatConsumer

USER_INFO_STORAGE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
    / "user_info_storage.ts"
)
PAGES = ("chat_consent", "explain_denial")

# Values no table or fixture holds, so finding one means this form put it
# there. The form does not check the shape of the phone, address or ZIP.
FIRST = "Zebulonkeep"
LAST = "Quillfeatherkeep"
EMAIL = "consent-keeps@example.com"
PHONE = "phone-mark-0199"
STREET = "street-mark 742 Evergreen"
CITY = "city-mark Springfieldton"
STATE = "state-mark Westmore"
ZIP = "zip-mark-97477"
REFERRAL = "Friend or Family"
REFERRAL_DETAILS = "details-mark Ottoline"
ADDRESS_PARTS = (STREET, CITY, STATE, ZIP)

# What the page says about each detail, word for word.
FORM_GOES_TO_THE_SERVER = (
    "Everything on this form goes to our server when you send it. Under each "
    "detail we say what we keep, and where. Our privacy policy has the rest."
)
NAME = (
    "This browser keeps your name, and the chat tries to take it out of your "
    "messages before they're sent. Our server keeps your name only if you "
    "tick the news box below."
)
EMAIL_KEPT = (
    "Our server keeps your email with the record that you agreed to these "
    "terms. With each chat it keeps a scrambled version (a hash), so you can "
    "ask us to delete your chats. This browser keeps your email too, and the "
    "chat tries to take it out of your messages."
)
PHONE_KEPT = (
    "Our server keeps your phone number only if you tick the news box below. "
    "This browser doesn't keep it, so the chat can't take it out of your "
    "messages."
)
ADDRESS = (
    "This browser keeps your address, and the chat tries to take your street "
    "address, city and ZIP code out of your messages before they're sent. It "
    "leaves your state in, because the answer can depend on it. Our server "
    "doesn't keep any of them."
)
NEWS = (
    "If you tick this, our server keeps your name, email, phone number and "
    "how you heard about us on our mailing list until you unsubscribe."
)


class _PageReader(HTMLParser):
    """The page's text with the whitespace collapsed, and each link to the
    privacy policy inside the form, with whether it sits inside a <label>.
    The footer's link is outside the form, so it does not count."""

    def __init__(self, policy_href: str):
        super().__init__()
        self.policy_href = policy_href
        self.forms_open = 0
        self.labels_open = 0
        self.chunks: list[str] = []
        self.policy_links_in_a_label: list[bool] = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.forms_open += 1
        elif tag == "label":
            self.labels_open += 1
        elif (
            tag == "a"
            and self.forms_open
            and dict(attrs).get("href") == self.policy_href
        ):
            self.policy_links_in_a_label.append(self.labels_open > 0)

    def handle_endtag(self, tag):
        if tag == "form" and self.forms_open:
            self.forms_open -= 1
        elif tag == "label" and self.labels_open:
            self.labels_open -= 1

    def handle_data(self, data):
        self.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.chunks).split())


def _read(html: str) -> _PageReader:
    reader = _PageReader(reverse("privacy_policy"))
    reader.feed(html)
    return reader


def _ts_function(name: str) -> str:
    """The source of one exported function in user_info_storage.ts."""
    source = USER_INFO_STORAGE.read_text()
    m = re.search(rf"export function {name}\(.*?\n}}\n", source, re.DOTALL)
    assert m, f"no {name} in user_info_storage.ts"
    return m.group(0)


def _what_the_server_keeps() -> str:
    """Every row of every table as text, with each saved session decoded."""
    sessions = import_module(settings.SESSION_ENGINE).SessionStore()
    kept = []
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for row in model.objects.all().values():
            if model._meta.label == "sessions.Session":
                row = sessions.decode(row["session_data"])
            kept.append(repr(row))
    return "\n".join(kept)


class ChatConsentSaysWhatItKeepsTest(TestCase):
    def _send(self, page: str, subscribe: bool) -> None:
        data = {
            "first_name": FIRST,
            "last_name": LAST,
            "email": EMAIL,
            "phone": PHONE,
            "address": STREET,
            "city": CITY,
            "state": STATE,
            "zip_code": ZIP,
            "referral_source": REFERRAL,
            "referral_source_details": REFERRAL_DETAILS,
            "tos_agreement": "on",
            "privacy_policy": "on",
        }
        if subscribe:
            data["subscribe"] = "on"
        if page == "explain_denial":
            data["denial_text"] = "My MRI was denied as not medically necessary."
        response = self.client.post(reverse(page), data)
        self.assertIn(response.status_code, (200, 302), page)
        self.assertTrue(self.client.session.get("consent_completed"), page)

    def _assert_the_page_says(self, sentence: str) -> None:
        for page in PAGES:
            with self.subTest(page=page):
                text = _read(self.client.get(reverse(page)).content.decode()).text
                self.assertIn(sentence, text)

    def test_the_details_note_links_the_privacy_policy_outside_a_label(self):
        for page in PAGES:
            with self.subTest(page=page):
                reader = _read(self.client.get(reverse(page)).content.decode())
                self.assertIn(False, reader.policy_links_in_a_label)

    def test_every_detail_on_the_form_goes_to_the_server(self):
        self._assert_the_page_says(FORM_GOES_TO_THE_SERVER)
        # A field without a name is never posted; every one here has one.
        for page in PAGES:
            with self.subTest(page=page):
                html = self.client.get(reverse(page)).content.decode()
                for field in UserConsentForm.base_fields:
                    self.assertIn(f'name="{field}"', html)

    def test_the_name_stays_off_the_server_without_the_news_box(self):
        self._assert_the_page_says(NAME)
        mappings = _ts_function("collectUserInfoFromForm")
        self.assertIn('firstName: getFieldValue("store_fname")', mappings)
        self.assertIn('lastName: getFieldValue("store_lname")', mappings)
        scrub = _ts_function("scrubPersonalInfo")
        self.assertIn("userInfo.firstName", scrub)
        self.assertIn("userInfo.lastName", scrub)
        for page in PAGES:
            with self.subTest(page=page):
                self._send(page, subscribe=False)
                kept = _what_the_server_keeps()
                self.assertNotIn(FIRST, kept)
                self.assertNotIn(LAST, kept)

    def test_the_name_is_kept_with_the_news_box(self):
        for page in PAGES:
            with self.subTest(page=page):
                self._send(page, subscribe=True)
                self.assertIn(f"{FIRST} {LAST}", _what_the_server_keeps())

    def test_the_email_is_kept_with_the_agreement(self):
        self._assert_the_page_says(EMAIL_KEPT)
        self.assertIn(
            'email: getFieldValue("email")', _ts_function("collectUserInfoFromForm")
        )
        self.assertIn("userInfo.email", _ts_function("scrubPersonalInfo"))
        for page in PAGES:
            with self.subTest(page=page):
                self._send(page, subscribe=False)
                session = self.client.session
                self.assertEqual(session["email"], EMAIL)
                self.assertTrue(session["consent_completed"])

    def test_each_chat_keeps_only_the_hash_and_a_delete_request_finds_it(self):
        chat = async_to_sync(OngoingChatConsumer()._get_or_create_chat)(
            None,
            chat_type=ChatType.PATIENT,
            session_key="consent-keeps-session",
            email=EMAIL,
        )
        self.assertEqual(chat.hashed_email, Denial.get_hashed_email(EMAIL))
        row = OngoingChat.objects.filter(pk=chat.pk).values().get()
        self.assertNotIn(EMAIL, repr(row))
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertFalse(OngoingChat.objects.filter(pk=chat.pk).exists())

    def test_the_phone_stays_off_the_server_and_out_of_the_browser(self):
        self._assert_the_page_says(PHONE_KEPT)
        self.assertNotIn("phone", _ts_function("collectUserInfoFromForm"))
        self.assertNotIn("userInfo.phone", _ts_function("scrubPersonalInfo"))
        for page in PAGES:
            with self.subTest(page=page):
                self._send(page, subscribe=False)
                self.assertNotIn(PHONE, _what_the_server_keeps())

    def test_the_address_stays_off_the_server_even_with_the_news_box(self):
        self._assert_the_page_says(ADDRESS)
        mappings = _ts_function("collectUserInfoFromForm")
        for field, element in (
            ("address", "store_street"),
            ("city", "store_city"),
            ("state", "store_state"),
            ("zipCode", "store_zip"),
        ):
            self.assertIn(f'{field}: getFieldValue("{element}")', mappings)
        scrub = _ts_function("scrubPersonalInfo")
        for field in ("address", "city", "zipCode"):
            self.assertIn(f"userInfo.{field}", scrub)
        # The scrubber's own comment says why the state stays in.
        self.assertNotIn("userInfo.state", scrub)
        for page in PAGES:
            for subscribe in (False, True):
                with self.subTest(page=page, subscribe=subscribe):
                    self._send(page, subscribe=subscribe)
                    kept = _what_the_server_keeps()
                    for part in ADDRESS_PARTS:
                        self.assertNotIn(part, kept)

    def test_the_news_box_keeps_what_it_says_until_unsubscribe(self):
        self._assert_the_page_says(NEWS)
        for page in PAGES:
            with self.subTest(page=page):
                self._send(page, subscribe=True)
                subscriber = MailingListSubscriber.objects.get(email=EMAIL)
                self.assertEqual(subscriber.name, f"{FIRST} {LAST}")
                self.assertEqual(subscriber.phone, PHONE)
                self.assertEqual(subscriber.referral_source, REFERRAL)
                self.assertEqual(subscriber.referral_source_details, REFERRAL_DETAILS)
                self.client.get(
                    reverse(
                        "unsubscribe",
                        kwargs={"token": subscriber.unsubscribe_token},
                    )
                )
                self.assertFalse(
                    MailingListSubscriber.objects.filter(email=EMAIL).exists()
                )

    def test_without_the_news_box_nothing_goes_on_the_mailing_list(self):
        for page in PAGES:
            with self.subTest(page=page):
                self._send(page, subscribe=False)
                self.assertFalse(
                    MailingListSubscriber.objects.filter(email=EMAIL).exists()
                )
                self.assertNotIn(REFERRAL_DETAILS, _what_the_server_keeps())

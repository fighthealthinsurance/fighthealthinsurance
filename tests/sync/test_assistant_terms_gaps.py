"""Gaps in the chat path's terms page and emailed way back
(assistant_terms_views.py, assistant_continue.py): who may agree, what the
person's choices keep, and which links open which case.

Reuses test_assistant_terms.py's helpers (functions and constants only, so
no test class runs twice).
"""

from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import assistant_continue, assistant_drafts
from fighthealthinsurance.assistant_terms_views import (
    AGREED_KEY,
    _agreed_here,
    _code_digest,
)
from fighthealthinsurance.models import (
    AssistantContinueLink,
    AssistantHandoff,
    ConsentRecord,
    Denial,
)
from tests.sync.test_assistant_terms import (
    AGREE,
    ALL_ON,
    EMAIL,
    IP,
    LETTER,
    TermsTestBase,
    chat_link,
    terms_form,
)


class AgreeGateTest(TermsTestBase):
    def test_the_agree_address_takes_no_get(self):
        response = self.client.get(AGREE)
        self.assertEqual(response.status_code, 405)

    def test_with_the_handoff_off_a_press_is_dead_and_uses_nothing(self):
        code, draft, _ = self.open_terms()
        with override_settings(MCP_PREPARE_APPEAL_ENABLED=False):
            response = self.client.post(AGREE, terms_form(code))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Denial.objects.exists())
        self.assertFalse(ConsentRecord.objects.exists())
        self.assertEqual(AssistantHandoff.objects.count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.WAITING)

    def test_a_press_from_another_browser_leaves_the_link_for_the_person(self):
        code, _, _ = self.open_terms()
        other = Client(HTTP_CF_CONNECTING_IP=IP)
        refused = other.post(AGREE, terms_form(code))
        self.assertEqual(refused.status_code, 404)
        # The person's own browser can still agree afterwards.
        agreed = self.client.post(AGREE, terms_form(code))
        self.assertEqual(agreed.status_code, 200)
        self.assertTemplateUsed(agreed, "assistant_agreed.html")
        self.assertEqual(Denial.objects.count(), 1)

    def test_a_missing_token_is_dead_and_makes_no_case(self):
        self.open_terms()
        data = terms_form("")
        response = self.client.post(AGREE, data)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Denial.objects.exists())

    def test_a_made_up_token_in_the_binding_browser_is_dead(self):
        self.open_terms()
        response = self.client.post(AGREE, terms_form("x" * 43))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Denial.objects.exists())
        self.assertFalse(ConsentRecord.objects.exists())


class AgreeChoicesTest(TermsTestBase):
    """What the person ticks or leaves unticked is what the case keeps."""

    def test_an_appeal_for_oneself_is_recorded_as_not_on_behalf(self):
        code, _, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code, on_behalf="me"))
        record = ConsentRecord.objects.get()
        self.assertFalse(record.on_behalf)

    def test_outside_ai_left_unticked_keeps_the_case_off_outside_models(self):
        code, _, _ = self.open_terms()
        data = terms_form(code)
        del data["use_external_models"]
        response = self.client.post(AGREE, data)
        self.assertTemplateUsed(response, "assistant_agreed.html")
        self.assertFalse(Denial.objects.get().use_external)

    def test_outside_ai_ticked_is_kept(self):
        code, _, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code))
        self.assertTrue(Denial.objects.get().use_external)

    def test_without_keep_my_email_the_address_is_kept_only_hashed(self):
        code, _, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code))
        denial = Denial.objects.get()
        self.assertFalse((denial.raw_email or "").strip())
        self.assertEqual(denial.hashed_email, Denial.get_hashed_email(EMAIL))

    def test_an_unknown_who_it_is_for_shows_the_page_again(self):
        code, draft, _ = self.open_terms()
        response = self.client.post(AGREE, terms_form(code, on_behalf="insurer"))
        self.assertTemplateUsed(response, "assistant_terms.html")
        self.assertFalse(Denial.objects.exists())
        self.assertEqual(AssistantHandoff.objects.count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.WAITING)

    def test_an_invalid_email_shows_the_page_again_and_counts_nothing(self):
        code, _, _ = self.open_terms()
        response = self.client.post(AGREE, terms_form(code, email="not-an-email"))
        self.assertTemplateUsed(response, "assistant_terms.html")
        self.assertFalse(Denial.objects.exists())
        self.assertEqual(AssistantHandoff.objects.count(), 1)


class AgreedHereTest(TestCase):
    """The agreed page is shown again only to the browser that agreed with
    that same link."""

    def _request(self, agreed):
        request = RequestFactory().post(AGREE)
        request.session = {} if agreed is None else {AGREED_KEY: agreed}
        return request

    def test_the_same_link_shows_what_the_page_said(self):
        request = self._request(
            {"code": _code_digest("link-a"), "started": True, "emailed": False}
        )
        self.assertEqual(
            _agreed_here(request, "link-a"), {"started": True, "emailed": False}
        )

    def test_another_link_in_the_same_browser_is_not_agreed(self):
        request = self._request(
            {"code": _code_digest("link-a"), "started": True, "emailed": True}
        )
        self.assertIsNone(_agreed_here(request, "link-b"))

    def test_an_empty_token_is_never_agreed(self):
        request = self._request(
            {"code": _code_digest(""), "started": True, "emailed": True}
        )
        self.assertIsNone(_agreed_here(request, ""))

    def test_a_session_value_of_another_shape_is_ignored(self):
        request = self._request(_code_digest("link-a"))
        self.assertIsNone(_agreed_here(request, "link-a"))

    def test_a_browser_that_never_agreed_is_not_agreed(self):
        self.assertIsNone(_agreed_here(self._request(None), "link-a"))

    def test_the_session_keeps_a_digest_not_the_link(self):
        self.assertNotIn("link-a", _code_digest("link-a"))
        self.assertEqual(len(_code_digest("link-a")), 64)


class ContinueGapsTest(TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))
        self.denial = Denial.objects.create(
            denial_text=LETTER,
            hashed_email=Denial.get_hashed_email(EMAIL),
            channel="assistant",
        )
        self.token = assistant_continue.mint(self.denial)
        self.path = reverse("assistant_continue")

    def _attempts(self) -> int:
        return AssistantContinueLink.objects.get().wrong_email_attempts

    def test_the_address_opens_whatever_its_case_and_spaces(self):
        response = self.client.post(
            self.path, {"token": self.token, "email": "  PERSON@Example.COM "}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["denial_uuid"], str(self.denial.uuid))

    def test_a_wrong_address_is_not_shown_back_on_the_page(self):
        typed = "rebecca.crumpler@example.com"
        response = self.client.post(self.path, {"token": self.token, "email": typed})
        self.assertTrue(response.context["wrong_email"])
        self.assertNotIn(typed, response.content.decode())
        self.assertNotIn("denial_uuid", self.client.session)

    def test_the_email_of_another_case_does_not_open_this_one(self):
        other_email = "elizabeth.blackwell@example.com"
        Denial.objects.create(
            denial_text=LETTER,
            hashed_email=Denial.get_hashed_email(other_email),
            channel="assistant",
        )
        response = self.client.post(
            self.path, {"token": self.token, "email": other_email}
        )
        self.assertTrue(response.context["wrong_email"])
        self.assertEqual(self._attempts(), 1)
        self.assertNotIn("denial_uuid", self.client.session)

    def test_an_address_that_is_not_an_email_counts_no_attempt(self):
        response = self.client.post(
            self.path, {"token": self.token, "email": "not an email"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["wrong_email"])
        self.assertEqual(self._attempts(), 0)

    def test_an_address_that_is_not_an_email_on_a_dead_link_is_404(self):
        response = self.client.post(
            self.path, {"token": "x" * 43, "email": "not an email"}
        )
        self.assertEqual(response.status_code, 404)

    def test_a_new_link_for_the_case_retires_the_old_one(self):
        newer = assistant_continue.mint(self.denial)
        old = self.client.post(self.path, {"token": self.token, "email": EMAIL})
        self.assertEqual(old.status_code, 404)
        new = self.client.post(self.path, {"token": newer, "email": EMAIL})
        self.assertEqual(new.status_code, 302)
        self.assertEqual(AssistantContinueLink.objects.count(), 1)

    def test_a_new_link_starts_with_no_wrong_attempts(self):
        self.client.post(self.path, {"token": self.token, "email": "a@example.com"})
        self.assertEqual(self._attempts(), 1)
        assistant_continue.mint(self.denial)
        self.assertEqual(self._attempts(), 0)

    def test_an_implausible_token_finds_nothing(self):
        for token in (None, "", 7, "x" * 129):
            with self.subTest(token=repr(token)[:20]):
                self.assertFalse(assistant_continue.plausible_token(token))
                self.assertIsNone(assistant_continue.live_link(token))
                self.assertEqual(
                    assistant_continue.open_case(token, EMAIL),
                    (assistant_continue.DEAD, None),
                )

    def test_the_link_keeps_only_a_digest_of_the_token(self):
        link = AssistantContinueLink.objects.get()
        self.assertNotEqual(link.token_digest, self.token)
        self.assertNotIn(self.token, link.token_digest)
        self.assertEqual(link.token_digest, assistant_continue.digest(self.token))

    def test_the_emailed_url_carries_the_token_after_the_hash(self):
        url = assistant_continue.url_for(self.token)
        path, _, fragment = url.partition("#")
        self.assertEqual(fragment, self.token)
        self.assertNotIn(self.token, path)

    def test_the_sweep_takes_revoked_links_and_keeps_live_ones(self):
        other = Denial.objects.create(
            denial_text=LETTER,
            hashed_email=Denial.get_hashed_email("other@example.com"),
            channel="assistant",
        )
        assistant_continue.mint(other)
        AssistantContinueLink.objects.filter(denial=other).update(token_digest=None)
        self.assertEqual(assistant_continue.sweep_expired(), 1)
        self.assertEqual(
            list(AssistantContinueLink.objects.values_list("denial_id", flat=True)),
            [self.denial.pk],
        )

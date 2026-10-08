"""The chat path's terms page, its per-address cap and the emailed way back
(assistant_terms_views.py, assistant_ip_limit.py, assistant_continue.py)."""

import asyncio
import datetime
import json
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from django.core import mail
from django.core.management import call_command
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from temporalio.exceptions import WorkflowAlreadyStartedError

from fighthealthinsurance import (
    assistant_continue,
    assistant_draft_tools,
    assistant_drafts,
    assistant_handoff,
    assistant_ip_limit,
)
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.assistant_handoff import claim_handoff, create_handoff
from fighthealthinsurance.assistant_terms_views import AssistantAgreeView, short_field
from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import (
    AssistantAgreementCount,
    AssistantContinueLink,
    AssistantDraft,
    AssistantHandoff,
    ConsentRecord,
    Denial,
    ProposedAppeal,
    SpendCounter,
    SpendReservation,
)
from fighthealthinsurance.temporal_client import (
    start_assistant_appeal_workflow as real_start,
)
from tests.sync.test_assistant_handoff import carried as site_form_fields
from tests.sync.test_assistant_handoff import open_forms

ALL_ON = dict(
    MCP_DRAFT_IN_CHAT_ENABLED=True,
    MCP_SERVER_ENABLED=True,
    MCP_PREPARE_APPEAL_ENABLED=True,
    MCP_HANDOFF_V2_ENABLED=True,
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_PAYLOAD_KEY="test-key",
    RECAPTCHA_TESTING=True,
)
LETTER = (
    "Dear member, your request for an MRI of the lower back was denied as "
    "not medically necessary."
)
LANDING = "/from-your-assistant"
AGREE = "/from-your-assistant/agree"
EMAIL = "person@example.com"
IP = "203.0.113.7"
TEMPORAL = "fighthealthinsurance.temporal_client"
START = f"{TEMPORAL}.start_assistant_appeal_workflow"


def chat_link(
    client_name: str = "Claude", procedure: str = "MRI", condition: str = "back pain"
):
    """A chat link and its waiting draft, the way draft_appeal_in_chat makes
    them: the procedure and condition only in the sealed link."""
    new = assistant_drafts.create_draft(None)
    handoff = create_handoff(
        LETTER,
        procedure,
        condition,
        kind="chat",
        client=client_name,
        draft=new.draft.pk,
    )
    return handoff.code, new.draft


def terms_form(code: str, **extra) -> dict:
    data = {
        "token": code,
        "denial_text": LETTER + " Edited.",
        "email": EMAIL,
        "zip": "94103",
        "on_behalf": "helping",
        "pii": "on",
        "privacy": "on",
        "tos": "on",
        "personalonly": "on",
        "use_external_models": "checked",
    }
    data.update(extra)
    return data


class TermsTestBase(TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))
        self.start = self.enterContext(patch(START, new_callable=AsyncMock))
        self.client = Client(HTTP_CF_CONNECTING_IP=IP)

    def open_terms(self, client=None, **sent):
        client = client or self.client
        code, draft = chat_link(**sent)
        bound = client.post(LANDING, {"token": code, "bind": "1"})
        self.assertEqual(bound.json(), {"bound": True})
        page = client.post(LANDING, {"token": code})
        return code, draft, page


class TermsPageTest(TermsTestBase):
    def test_a_chat_link_opens_the_terms_page_with_the_letter_and_keeps_the_link(self):
        code, _, page = self.open_terms()
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "assistant_terms.html")
        body = page.content.decode()
        self.assertIn(LETTER, body)
        self.assertIn("only the person may agree", body)
        self.assertIn('id="scrub-2"', body)
        self.assertIn("scrub.bundle.js", body)
        self.assertEqual(AssistantHandoff.objects.count(), 1)

    def test_the_terms_page_loads_no_third_party_script(self):
        _, _, page = self.open_terms()
        body = page.content.decode()
        self.assertNotIn("googletagmanager", body)
        self.assertNotIn("bat.bing.com", body)

    def test_the_boxes_are_the_intake_pages_own_words(self):
        from fighthealthinsurance.consent import BOXES

        _, _, page = self.open_terms()
        for name in BOXES:
            self.assertIn(f'name="{name}"', page.content.decode())

    def test_with_the_chat_path_off_a_chat_link_opens_the_site_form(self):
        with override_settings(MCP_DRAFT_IN_CHAT_ENABLED=False):
            code, _, page = self.open_terms()
        self.assertTemplateUsed(page, "scrub.html")
        self.assertEqual(AssistantHandoff.objects.count(), 0)

    def test_a_site_link_still_opens_the_site_form(self):
        handoff = create_handoff(LETTER, kind="site")
        self.client.post(LANDING, {"token": handoff.code, "bind": "1"})
        page = self.client.post(LANDING, {"token": handoff.code})
        self.assertTemplateUsed(page, "scrub.html")

    def test_another_browser_cannot_agree(self):
        code, _, _ = self.open_terms()
        other = Client(HTTP_CF_CONNECTING_IP=IP)
        response = other.post(AGREE, terms_form(code))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Denial.objects.exists())

    def test_the_form_fields_are_hidden_from_error_reports(self):
        code, _, _ = self.open_terms()
        response = self.client.post(AGREE, terms_form(code, pii=""))
        hidden = response.wsgi_request.sensitive_post_parameters
        for name in ("token", "email", "denial_text", "procedure", "condition"):
            self.assertIn(name, hidden)


class AgreeTest(TermsTestBase):
    def test_agreeing_makes_the_case_records_the_boxes_and_starts_the_letters(self):
        code, draft, _ = self.open_terms()
        response = self.client.post(AGREE, terms_form(code))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "assistant_agreed.html")
        self.assertTrue(response.context["started"])
        denial = Denial.objects.get()
        self.assertEqual(denial.channel, "assistant")
        self.assertEqual(denial.denial_text, LETTER + " Edited.")
        record = ConsentRecord.objects.get(denial=denial)
        self.assertEqual(
            (
                record.channel,
                record.finish_in,
                record.on_behalf,
                record.assistant_client,
            ),
            ("assistant", "chat", True, "Claude"),
        )
        self.assertTrue(all(box["ticked"] for box in record.boxes))
        draft.refresh_from_db()
        self.assertEqual(draft.denial_id, denial.pk)
        self.assertEqual(draft.status, assistant_drafts.READING)
        self.assertGreater(draft.expires_at, timezone.now() + timedelta(hours=23))
        self.start.assert_awaited_once_with(denial.hashed_email, str(denial.uuid))
        self.assertEqual(AssistantHandoff.objects.count(), 0)
        self.assertEqual(AssistantAgreementCount.objects.get().count, 1)
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 1)

    def test_the_continue_link_is_emailed_once_and_says_nothing_in_the_url(self):
        code, _, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [EMAIL])
        url = next(w for w in message.body.split() if "your-appeal-letters" in w)
        path, _, token = url.partition("#")
        self.assertEqual(len(token), 43)
        self.assertNotIn("person", path)
        link = AssistantContinueLink.objects.get()
        self.assertEqual(link.token_digest, assistant_continue.digest(token))
        self.assertNotIn(token, json.dumps([link.token_digest]))
        # The link is used up, so a second press sends nothing more.
        self.client.post(AGREE, terms_form(code))
        self.assertEqual(len(mail.outbox), 1)

    def test_the_continue_email_goes_alone_and_its_button_opens_the_same_link(self):
        code, _, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code))
        message = mail.outbox[0]
        self.assertIn("Fight Health Insurance Support", message.from_email)
        # No staff copy: it would hold the link beside the address it asks for.
        self.assertEqual((message.cc, message.bcc), ([], []))
        url = next(w for w in message.body.split() if "your-appeal-letters" in w)
        ((html, _),) = message.alternatives
        self.assertIn(f'href="{url}"', html)
        # The days it says the link works are the link's real lifetime.
        for part in (message.body, html):
            self.assertIn(f"{assistant_continue.link_days()} days", part)

    def test_a_continue_email_that_fails_to_send_returns_false(self):
        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=OSError("smtp down"),
        ):
            self.assertFalse(assistant_continue.send(EMAIL, "a-token"))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_second_press_after_agreeing_shows_the_agreed_page_again(self):
        code, _, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code))
        again = self.client.post(AGREE, terms_form(code))
        self.assertEqual(again.status_code, 200)
        self.assertTemplateUsed(again, "assistant_agreed.html")
        self.assertTrue(again.context["started"])
        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_press_on_a_link_used_elsewhere_says_where_to_look(self):
        code, _, _ = self.open_terms()
        binder = self.client.cookies["fhi_handoff_binder"].value
        self.assertIsNotNone(claim_handoff(code, binder=binder))
        response = self.client.post(AGREE, terms_form(code))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.context["after_a_press"])
        self.assertContains(response, "it may have gone through", status_code=404)

    def test_the_press_that_loses_the_link_to_another_gives_everything_back(self):
        """Two presses at once: the other press uses the link after this one
        counted and reserved, but before it could use the link itself."""
        code, draft, _ = self.open_terms()
        binder = self.client.cookies["fhi_handoff_binder"].value
        real_take = assistant_ip_limit.take

        def take_then_the_other_press_uses_the_link(request):
            taken = real_take(request)
            self.assertIsNotNone(claim_handoff(code, binder=binder))
            return taken

        with patch.object(
            assistant_ip_limit, "take", take_then_the_other_press_uses_the_link
        ):
            response = self.client.post(AGREE, terms_form(code))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.context["after_a_press"])
        self.assertEqual(AssistantAgreementCount.objects.get().count, 0)
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 0)
        self.assertIsNotNone(SpendReservation.objects.get().released_at)
        self.assertFalse(Denial.objects.exists())
        draft.refresh_from_db()
        self.assertIsNone(draft.denial_id)
        self.start.assert_not_called()

    def test_a_missing_box_shows_the_page_again_and_counts_nothing(self):
        code, _, _ = self.open_terms()
        response = self.client.post(AGREE, terms_form(code, tos=""))
        self.assertTemplateUsed(response, "assistant_terms.html")
        self.assertIn("Edited.", response.content.decode())
        self.assertFalse(Denial.objects.exists())
        self.assertEqual(AssistantHandoff.objects.count(), 1)
        self.assertFalse(AssistantAgreementCount.objects.exists())

    def test_who_it_is_for_is_required(self):
        code, _, _ = self.open_terms()
        data = terms_form(code)
        del data["on_behalf"]
        response = self.client.post(AGREE, data)
        self.assertTemplateUsed(response, "assistant_terms.html")
        self.assertFalse(Denial.objects.exists())

    def test_at_the_per_address_cap_the_filled_site_form_opens_instead(self):
        with override_settings(MCP_ASSISTANT_PER_IP_DAILY=1):
            first, _, _ = self.open_terms()
            self.client.post(AGREE, terms_form(first))
            code, draft, _ = self.open_terms()
            response = self.client.post(AGREE, terms_form(code))
        self.assertTemplateUsed(response, "scrub.html")
        self.assertIn("Edited.", response.content.decode())
        self.assertIn(EMAIL, response.content.decode())
        self.assertEqual(Denial.objects.count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.ON_SITE)
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 1)
        self.assertIn(
            site_form_fields(response)["assistant_form"], open_forms(self.client)
        )

    def test_a_count_the_database_refuses_opens_the_filled_site_form(self):
        code, draft, _ = self.open_terms()
        with patch.object(
            AssistantAgreementCount.objects,
            "get_or_create",
            side_effect=DatabaseError("database away"),
        ):
            response = self.client.post(AGREE, terms_form(code))
        self.assertTemplateUsed(response, "scrub.html")
        self.assertIn("Edited.", response.content.decode())
        self.assertFalse(Denial.objects.exists())
        self.assertFalse(SpendCounter.objects.filter(amount__gt=0).exists())
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.ON_SITE)

    def test_another_address_has_its_own_count(self):
        with override_settings(MCP_ASSISTANT_PER_IP_DAILY=1):
            first, _, _ = self.open_terms()
            self.client.post(AGREE, terms_form(first))
            other = Client(HTTP_CF_CONNECTING_IP="198.51.100.9")
            code, _, _ = self.open_terms(other)
            response = other.post(AGREE, terms_form(code))
        self.assertTemplateUsed(response, "assistant_agreed.html")
        self.assertEqual(Denial.objects.count(), 2)

    def test_agreeing_ties_the_reserved_generation_to_the_draft(self):
        code, draft, _ = self.open_terms()
        self.client.post(AGREE, terms_form(code))
        draft.refresh_from_db()
        self.assertIsNotNone(draft.spend_reservation)
        self.assertEqual(draft.spend_reservation.name, "fhi:assistant")
        self.assertIsNone(draft.spend_reservation.released_at)

    def test_a_spent_budget_opens_the_site_form_and_gives_the_place_back(self):
        code, draft, _ = self.open_terms()
        with patch.object(spend, "reserve_generation", return_value=None):
            response = self.client.post(AGREE, terms_form(code))
        self.assertTemplateUsed(response, "scrub.html")
        self.assertFalse(Denial.objects.exists())
        self.assertEqual(AssistantAgreementCount.objects.get().count, 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.ON_SITE)

    def test_a_link_without_a_waiting_draft_opens_the_site_form(self):
        code, draft, _ = self.open_terms()
        draft.delete()
        response = self.client.post(AGREE, terms_form(code))
        self.assertTemplateUsed(response, "scrub.html")
        self.assertFalse(AssistantAgreementCount.objects.exists())
        self.assertFalse(SpendCounter.objects.filter(amount__gt=0).exists())

    def test_a_workflow_that_does_not_start_gives_the_generation_back(self):
        code, draft, _ = self.open_terms()
        self.start.side_effect = RuntimeError("no temporal")
        response = self.client.post(AGREE, terms_form(code))
        self.assertTemplateUsed(response, "assistant_agreed.html")
        self.assertFalse(response.context["started"])
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 0)
        self.assertEqual(AssistantAgreementCount.objects.get().count, 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.STOPPED)

    def test_a_temporal_that_hangs_shows_the_not_started_page_soon(self):
        code, draft, _ = self.open_terms()

        async def hangs(*args, **kwargs):
            await asyncio.sleep(5)

        # The real start, so the wait covers connecting too.
        self.start.side_effect = real_start
        with patch(f"{TEMPORAL}.get_temporal_client", hangs), patch(
            f"{TEMPORAL}.ASSISTANT_REQUEST_WAIT_SECONDS", 0.05
        ):
            began = time.monotonic()
            response = self.client.post(AGREE, terms_form(code))
            took = time.monotonic() - began
        self.assertLess(took, 3)
        self.assertTemplateUsed(response, "assistant_agreed.html")
        self.assertFalse(response.context["started"])
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 0)
        self.assertEqual(AssistantAgreementCount.objects.get().count, 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.STOPPED)

    def test_a_workflow_already_running_counts_as_started(self):
        code, draft, _ = self.open_terms()
        self.start.side_effect = WorkflowAlreadyStartedError(
            "assistant-appeal-x", "AssistantAppealWorkflow"
        )
        response = self.client.post(AGREE, terms_form(code))
        self.assertTrue(response.context["started"])
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 1)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.READING)

    def test_a_case_that_fails_to_save_tells_the_assistant_to_finish_on_the_site(
        self,
    ):
        code, draft, _ = self.open_terms()
        with patch.object(
            AssistantAgreeView, "_create_denial", side_effect=RuntimeError("no case")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(AGREE, terms_form(code))
        self.assertEqual(AssistantHandoff.objects.count(), 0)
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 0)
        self.assertEqual(AssistantAgreementCount.objects.get().count, 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.STOPPED)
        self.assertEqual(
            assistant_draft_tools.view(draft)["next"],
            assistant_draft_tools.FINISH_ON_SITE,
        )
        self.start.assert_not_called()

    def test_a_case_that_fails_to_save_still_shows_its_own_error(self):
        code, _, _ = self.open_terms()
        with patch.object(
            AssistantAgreeView, "_create_denial", side_effect=RuntimeError("no case")
        ), patch.object(
            assistant_drafts, "set_status", side_effect=DatabaseError("away")
        ):
            with self.assertRaisesMessage(RuntimeError, "no case"):
                self.client.post(AGREE, terms_form(code))

    def test_a_continue_link_that_fails_gives_everything_back(self):
        code, draft, _ = self.open_terms()
        with patch.object(
            assistant_continue, "mint", side_effect=RuntimeError("no link")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(AGREE, terms_form(code))
        self.assertEqual(SpendCounter.objects.get(name="fhi:assistant").amount, 0)
        self.assertEqual(AssistantAgreementCount.objects.get().count, 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.STOPPED)
        self.start.assert_not_called()

    def test_finish_on_this_site_opens_the_site_form_with_the_letter_as_edited(self):
        code, draft, _ = self.open_terms()
        response = self.client.post(
            AGREE, {"token": code, "finish": "site", "denial_text": "My own words."}
        )
        self.assertTemplateUsed(response, "scrub.html")
        self.assertIn("My own words.", response.content.decode())
        self.assertEqual(AssistantHandoff.objects.count(), 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.ON_SITE)
        self.assertFalse(Denial.objects.exists())

    def test_an_appeal_finished_on_this_site_still_names_the_assistant(self):
        code, _, _ = self.open_terms()
        site_form = self.client.post(AGREE, terms_form(code, finish="site"))
        response = self.client.post(
            reverse("scan"),
            {
                **site_form_fields(site_form),
                "email": EMAIL,
                "denial_text": "My own words.",
                "zip": "94103",
                "pii": "on",
                "privacy": "on",
                "tos": "on",
                "personalonly": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        denial = Denial.objects.get()
        self.assertEqual(denial.channel, "site")
        record = ConsentRecord.objects.get(denial=denial)
        self.assertEqual(
            (
                record.channel,
                record.finish_in,
                record.on_behalf,
                record.assistant_client,
            ),
            ("assistant", "site", False, "Claude"),
        )
        self.assertFalse(
            SpendCounter.objects.filter(name="fhi:assistant", amount__gt=0).exists()
        )


def field_values(response, *names) -> dict:
    """The values of the named inputs on a rendered page."""
    soup = BeautifulSoup(response.content.decode(), "html.parser")
    out = {}
    for name in names:
        field = soup.find("input", attrs={"name": name})
        out[name] = None if field is None else field.get("value", "")
    return out


class ShortFieldsTest(TermsTestBase):
    """What the assistant sent on what was denied and the condition, shown
    under the letter for the person to change or clear."""

    def draft_after_agreeing(self, **fields):
        code, draft, _ = self.open_terms()
        response = self.client.post(AGREE, terms_form(code, **fields))
        self.assertTemplateUsed(response, "assistant_agreed.html")
        draft.refresh_from_db()
        return draft

    def test_the_page_shows_what_the_assistant_sent_filled_in(self):
        _, _, page = self.open_terms()
        soup = BeautifulSoup(page.content.decode(), "html.parser")
        procedure = soup.find(id="assistant_procedure")
        condition = soup.find(id="assistant_condition")
        self.assertEqual((procedure["value"], condition["value"]), ("MRI", "back pain"))
        labels = {
            label["for"]: label.get_text(strip=True)
            for label in soup.find_all("label", attrs={"for": True})
        }
        self.assertEqual(labels["assistant_procedure"], "What was denied")
        self.assertEqual(labels["assistant_condition"], "The condition")
        self.assertIn("You can change or clear them.", page.content.decode())

    def test_the_fields_are_kept_out_of_the_browser_and_the_letter_scrub(self):
        # scrub.ts keeps store_ and email fields in the browser and takes
        # their words out of the letter; these must be neither.
        _, _, page = self.open_terms()
        soup = BeautifulSoup(page.content.decode(), "html.parser")
        for name in ("procedure", "condition"):
            field = soup.find("input", attrs={"name": name, "type": "text"})
            self.assertFalse(field["id"].startswith(("store_", "email")))

    def test_edited_values_reach_the_draft(self):
        draft = self.draft_after_agreeing(
            procedure="MRI of the lumbar spine", condition="sciatica"
        )
        self.assertEqual(
            (draft.procedure, draft.condition), ("MRI of the lumbar spine", "sciatica")
        )

    def test_values_left_as_shown_reach_the_draft(self):
        draft = self.draft_after_agreeing(procedure="MRI", condition="back pain")
        self.assertEqual((draft.procedure, draft.condition), ("MRI", "back pain"))

    def test_cleared_values_store_blank(self):
        draft = self.draft_after_agreeing(procedure="", condition="  ")
        self.assertEqual((draft.procedure, draft.condition), ("", ""))

    def test_over_long_values_and_hidden_characters_are_cleaned(self):
        draft = self.draft_after_agreeing(
            procedure="MRI\u202e\x07 of the\u200b back " + "x" * 200,
            condition="  back\r\npain\u2066\U000e0041 ",
        )
        self.assertEqual(
            draft.procedure,
            ("MRI of the back " + "x" * 200)[: assistant_drafts.FIELD_MAX_CHARS],
        )
        self.assertEqual(draft.condition, "back pain")

    def test_short_field_cleans_like_prepare_appeal_and_cuts_at_the_draft_limit(self):
        self.assertEqual(short_field("A\u2060B\U000e0101 C\x00"), "AB C")
        self.assertEqual(short_field("word " * 40), ("word " * 16).strip())
        self.assertLessEqual(
            len(short_field("y" * 500)), assistant_drafts.FIELD_MAX_CHARS
        )

    def test_a_long_value_the_assistant_sent_shows_as_it_will_be_kept(self):
        _, _, page = self.open_terms(procedure="p" * 150)
        self.assertEqual(
            field_values(page, "procedure")["procedure"],
            "p" * assistant_drafts.FIELD_MAX_CHARS,
        )

    def test_a_press_from_a_page_without_the_fields_keeps_what_was_sent(self):
        draft = self.draft_after_agreeing()
        self.assertEqual((draft.procedure, draft.condition), ("MRI", "back pain"))

    def test_a_page_sent_back_keeps_the_values_as_edited(self):
        code, _, _ = self.open_terms()
        response = self.client.post(
            AGREE, terms_form(code, tos="", procedure="CT scan", condition="")
        )
        self.assertTemplateUsed(response, "assistant_terms.html")
        self.assertEqual(
            field_values(response, "procedure", "condition"),
            {"procedure": "CT scan", "condition": ""},
        )

    def test_finish_on_this_site_carries_them_as_edited(self):
        code, _, page = self.open_terms()
        # Without the page's script, the button's form sends what was shown.
        self.assertEqual(
            {
                tag["name"]: tag["value"]
                for tag in BeautifulSoup(page.content.decode(), "html.parser")
                .find(id="assistant-finish-on-site")
                .find_all("input", attrs={"name": ["procedure", "condition"]})
            },
            {"procedure": "MRI", "condition": "back pain"},
        )
        response = self.client.post(
            AGREE,
            {
                "token": code,
                "finish": "site",
                "denial_text": "My own words.",
                "procedure": "MRI of the \u202elumbar spine",
                "condition": "",
            },
        )
        self.assertTemplateUsed(response, "scrub.html")
        self.assertEqual(
            field_values(response, "default_procedure", "default_condition"),
            {"default_procedure": "MRI of the lumbar spine", "default_condition": ""},
        )

    def test_finish_on_this_site_with_both_cleared_carries_none(self):
        """Carried blank, and said to be given: blank means none, not "keep
        what an earlier form left"."""
        code, _, _ = self.open_terms()
        response = self.client.post(
            AGREE,
            {"token": code, "finish": "site", "procedure": "", "condition": ""},
        )
        self.assertTemplateUsed(response, "scrub.html")
        self.assertEqual(
            field_values(
                response, "default_procedure", "default_condition", "defaults_given"
            ),
            {"default_procedure": "", "default_condition": "", "defaults_given": "1"},
        )

    def submit_on_site(self, fields: dict, email: str = EMAIL, **extra):
        response = self.client.post(
            reverse("scan"),
            {
                **fields,
                "email": email,
                "denial_text": "My own words about the denial.",
                "zip": "94103",
                "pii": "on",
                "privacy": "on",
                "tos": "on",
                "personalonly": "on",
                **extra,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response

    def start_a_guided_case(self, email: str = EMAIL):
        """A case started earlier in this browser from a treatment guide,
        not finished: the session keeps the guide's treatment, and /process
        reuses the case for the same email."""
        self.submit_on_site(
            {
                "default_procedure": "PrEP",
                "default_condition": "HIV prevention",
                "microsite_title": "PrEP",
            },
            email=email,
        )
        self.assertEqual(self.client.session["default_procedure"], "PrEP")
        return Denial.objects.get(hashed_email=Denial.get_hashed_email(email))

    def test_clearing_both_stays_cleared_when_the_case_is_reused(self):
        """Both cleared on the terms page, then the site form: through
        Finish on this site instead, or through Agree once the v2 flag went
        off after the page opened, which opens the site form too, without
        naming the assistant. Either way the reused case keeps no earlier
        treatment."""
        for v2_turned_off in (False, True):
            with self.subTest(v2_turned_off=v2_turned_off):
                self.client = Client(HTTP_CF_CONNECTING_IP=IP)
                email = f"cleared-{str(v2_turned_off).lower()}@example.com"
                earlier = self.start_a_guided_case(email)
                code, _, _ = self.open_terms()
                with override_settings(MCP_HANDOFF_V2_ENABLED=not v2_turned_off):
                    site_form = self.client.post(
                        AGREE,
                        terms_form(
                            code,
                            procedure="",
                            condition="",
                            **({} if v2_turned_off else {"finish": "site"}),
                        ),
                    )
                    self.assertTemplateUsed(site_form, "scrub.html")
                    self.submit_on_site(site_form_fields(site_form), email=email)
                self.assertEqual(
                    list(Denial.objects.filter(hashed_email=earlier.hashed_email)),
                    [earlier],
                    "reused",
                )
                for key in (
                    "default_procedure",
                    "default_condition",
                    "microsite_title",
                    "microsite_slug",
                ):
                    with self.subTest(key=key):
                        self.assertNotIn(key, self.client.session)
                # With v2 on, the case names the assistant whose link opened
                # the form; a form opened with it off names none.
                record = ConsentRecord.objects.filter(denial=earlier).latest("pk")
                self.assertEqual(
                    (record.channel, record.assistant_client),
                    ("site", "") if v2_turned_off else ("assistant", "Claude"),
                )

    def test_clearing_both_stays_cleared_through_an_error_and_a_retry(self):
        earlier = self.start_a_guided_case()
        code, _, _ = self.open_terms()
        site_form = self.client.post(
            AGREE, terms_form(code, finish="site", procedure="", condition="")
        )
        sent_back = self.submit_on_site(
            site_form_fields(site_form), email="not-an-email"
        )
        self.assertTemplateUsed(sent_back, "scrub.html")
        retry = site_form_fields(sent_back)
        self.assertEqual(
            {name: retry[name] for name in ("default_procedure", "default_condition")},
            {"default_procedure": "", "default_condition": ""},
        )
        self.assertEqual(retry["defaults_given"], "1")
        self.submit_on_site(retry)
        self.assertEqual(list(Denial.objects.all()), [earlier], "reused")
        self.assertNotIn("default_procedure", self.client.session)
        self.assertNotIn("default_condition", self.client.session)

    def test_a_treatment_left_in_replaces_an_earlier_one_on_a_reused_case(self):
        self.start_a_guided_case()
        code, _, _ = self.open_terms()
        site_form = self.client.post(
            AGREE, terms_form(code, finish="site", procedure="CT scan", condition="")
        )
        self.submit_on_site(site_form_fields(site_form))
        self.assertEqual(
            (
                self.client.session["default_procedure"],
                self.client.session["default_condition"],
            ),
            ("CT scan", ""),
        )

    def test_the_site_form_a_refusal_opens_carries_them_as_edited(self):
        code, _, _ = self.open_terms()
        with patch.object(spend, "reserve_generation", return_value=None):
            response = self.client.post(
                AGREE, terms_form(code, procedure="Physical therapy")
            )
        self.assertTemplateUsed(response, "scrub.html")
        self.assertEqual(
            field_values(response, "default_procedure", "default_condition"),
            {"default_procedure": "Physical therapy", "default_condition": "back pain"},
        )

    def test_the_edited_treatment_reaches_the_case_finished_on_this_site(self):
        code, _, _ = self.open_terms()
        site_form = self.client.post(
            AGREE,
            terms_form(code, finish="site", procedure="Lumbar MRI", condition=""),
        )
        carried = field_values(site_form, "default_procedure", "default_condition")
        response = self.client.post(
            reverse("scan"),
            {
                "email": EMAIL,
                "denial_text": "My own words.",
                "zip": "94103",
                "pii": "on",
                "privacy": "on",
                "tos": "on",
                "personalonly": "on",
                **carried,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (
                self.client.session["default_procedure"],
                self.client.session["default_condition"],
            ),
            ("Lumbar MRI", ""),
        )


class AgreeRefusalTest(TermsTestBase):
    def test_switching_keeps_the_outside_ai_choice_as_it_was(self):
        code, _, _ = self.open_terms()
        response = self.client.post(
            AGREE,
            {
                "token": code,
                "finish": "site",
                "denial_text": "My own words.",
                "email": EMAIL,
                "use_external_models": "",
            },
        )
        self.assertTemplateUsed(response, "scrub.html")
        body = response.content.decode()
        self.assertIn(EMAIL, body)
        self.assertNotRegex(body, r'id="use_external_models"[^>]*\schecked[\s>]')

    def test_an_unticked_bot_check_asks_again_on_the_terms_page(self):
        code, draft, _ = self.open_terms()
        with patch(
            "fighthealthinsurance.forms.ReCaptchaOptionalMixin._is_recaptcha_enabled",
            return_value=True,
        ):
            # Stubbed: a rendered widget here can hang a later sync test file.
            with patch(
                "fighthealthinsurance.assistant_terms_views.render_terms",
                return_value=HttpResponse("terms"),
            ) as render:
                self.client.post(AGREE, terms_form(code))
        form = render.call_args.args[3]
        self.assertEqual(
            [e.code for e in form.errors.as_data()["captcha"]], ["required"]
        )
        self.assertFalse(Denial.objects.exists())
        self.assertFalse(AssistantAgreementCount.objects.exists())
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.WAITING)

    def test_a_tick_that_ran_out_or_was_sent_twice_asks_again(self):
        from django_recaptcha.client import RecaptchaResponse

        code, draft, _ = self.open_terms()
        with patch(
            "fighthealthinsurance.forms.ReCaptchaOptionalMixin._is_recaptcha_enabled",
            return_value=True,
        ), patch(
            "django_recaptcha.fields.client.submit",
            return_value=RecaptchaResponse(
                is_valid=False, error_codes=["timeout-or-duplicate"]
            ),
        ), patch(
            "fighthealthinsurance.assistant_terms_views.render_terms",
            return_value=HttpResponse("terms"),
        ) as render:
            self.client.post(
                AGREE, {**terms_form(code), "g-recaptcha-response": "stale"}
            )
        form = render.call_args.args[3]
        self.assertEqual(
            [e.code for e in form.errors.as_data()["captcha"]],
            [core_forms.CAPTCHA_EXPIRED],
        )
        self.assertFalse(Denial.objects.exists())
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.WAITING)

    def test_a_ticked_box_agrees(self):
        from django_recaptcha.client import RecaptchaResponse

        code, _, _ = self.open_terms()
        with patch(
            "fighthealthinsurance.forms.ReCaptchaOptionalMixin._is_recaptcha_enabled",
            return_value=True,
        ), patch(
            "django_recaptcha.fields.client.submit",
            return_value=RecaptchaResponse(is_valid=True),
        ):
            response = self.client.post(
                AGREE, {**terms_form(code), "g-recaptcha-response": "ok"}
            )
        self.assertTemplateUsed(response, "assistant_agreed.html")
        self.assertTrue(Denial.objects.exists())

    def test_google_out_of_reach_asks_again(self):
        from urllib.error import URLError

        code, draft, _ = self.open_terms()
        with patch(
            "fighthealthinsurance.forms.ReCaptchaOptionalMixin._is_recaptcha_enabled",
            return_value=True,
        ), patch(
            "django_recaptcha.fields.client.submit",
            side_effect=URLError("timed out"),
        ), patch(
            "fighthealthinsurance.assistant_terms_views.render_terms",
            return_value=HttpResponse("terms"),
        ) as render:
            self.client.post(AGREE, {**terms_form(code), "g-recaptcha-response": "t"})
        form = render.call_args.args[3]
        self.assertEqual(
            [e.code for e in form.errors.as_data()["captcha"]], ["captcha_error"]
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.WAITING)

    def test_a_tick_sent_twice_after_the_link_was_used_says_where_to_look(self):
        from django_recaptcha.client import RecaptchaResponse

        code, _, _ = self.open_terms()
        binder = self.client.cookies["fhi_handoff_binder"].value
        claim_handoff(code, binder=binder)
        with patch(
            "fighthealthinsurance.forms.ReCaptchaOptionalMixin._is_recaptcha_enabled",
            return_value=True,
        ), patch(
            "django_recaptcha.fields.client.submit",
            return_value=RecaptchaResponse(
                is_valid=False, error_codes=["timeout-or-duplicate"]
            ),
        ):
            response = self.client.post(
                AGREE, {**terms_form(code), "g-recaptcha-response": "twice"}
            )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.context["after_a_press"])

    def test_a_failed_bot_check_opens_the_site_form(self):
        from django_recaptcha.client import RecaptchaResponse

        code, draft, _ = self.open_terms()
        with patch(
            "fighthealthinsurance.forms.ReCaptchaOptionalMixin._is_recaptcha_enabled",
            return_value=True,
        ), patch(
            "django_recaptcha.fields.client.submit",
            return_value=RecaptchaResponse(is_valid=False),
        ):
            response = self.client.post(
                AGREE, {**terms_form(code), "g-recaptcha-response": "rejected"}
            )
        self.assertTemplateUsed(response, "scrub.html")
        self.assertFalse(Denial.objects.exists())
        self.assertFalse(AssistantAgreementCount.objects.exists())
        draft.refresh_from_db()
        self.assertEqual(draft.status, assistant_drafts.ON_SITE)

    def test_the_denial_is_an_assistant_one_before_background_work_starts(self):
        seen = []

        def dispatch(denial_id, **kwargs):
            seen.append(Denial.objects.get(denial_id=denial_id).channel)

        code, _, _ = self.open_terms()
        with patch(
            "fighthealthinsurance.ml.ml_speculative_appeals_helper.dispatch_speculative_appeals",
            side_effect=dispatch,
        ):
            self.client.post(AGREE, terms_form(code))
        self.assertEqual(seen, ["assistant"])


class DraftLinkTest(TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))

    def test_a_chat_link_names_its_draft(self):
        code, draft = chat_link()
        content = claim_handoff(code)
        self.assertEqual(content.kind, "chat")
        self.assertEqual(assistant_drafts.waiting_draft(content.draft), draft)

    def test_a_draft_is_agreed_to_once(self):
        _, draft = chat_link()
        denial = Denial.objects.create(
            denial_text=LETTER, hashed_email=Denial.get_hashed_email(EMAIL)
        )
        self.assertTrue(assistant_drafts.agree(draft, denial))
        self.assertFalse(assistant_drafts.agree(draft, denial))
        self.assertIsNone(assistant_drafts.waiting_draft(draft.pk))

    def test_an_expired_or_odd_draft_reference_finds_nothing(self):
        _, draft = chat_link()
        self.assertIsNone(assistant_drafts.waiting_draft(True))
        self.assertIsNone(assistant_drafts.waiting_draft(str(draft.pk)))
        AssistantDraft.objects.filter(pk=draft.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertIsNone(assistant_drafts.waiting_draft(draft.pk))

    def test_without_v2_a_link_carries_no_draft(self):
        with override_settings(MCP_HANDOFF_V2_ENABLED=False):
            handoff = create_handoff(LETTER, kind="chat", draft=7)
        self.assertIsNone(claim_handoff(handoff.code).draft)


class PerAddressKeyTest(TestCase):
    def request(self, ip=None):
        extra = {"HTTP_CF_CONNECTING_IP": ip} if ip is not None else {}
        return RequestFactory().post("/", **extra)

    def test_ipv6_counts_by_its_64(self):
        a = assistant_ip_limit.address_of(self.request("2001:db8:1:2::1"))
        b = assistant_ip_limit.address_of(self.request("2001:db8:1:2:ffff::9"))
        c = assistant_ip_limit.address_of(self.request("2001:db8:1:3::1"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_an_ipv4_address_written_as_ipv6_counts_as_the_ipv4_address(self):
        mapped = self.request(f"::ffff:{IP}")
        self.assertEqual(assistant_ip_limit.address_of(mapped), IP)
        with override_settings(MCP_ASSISTANT_PER_IP_DAILY=1):
            self.assertIsNotNone(assistant_ip_limit.take(self.request(IP)))
            self.assertIsNone(assistant_ip_limit.take(mapped))

    def test_a_count_the_database_refuses_is_no_place(self):
        with patch.object(
            AssistantAgreementCount.objects,
            "get_or_create",
            side_effect=DatabaseError("database away"),
        ):
            self.assertIsNone(assistant_ip_limit.take(self.request(IP)))

    def test_a_give_back_the_database_refuses_does_not_raise(self):
        taken = assistant_ip_limit.take(self.request(IP))
        with patch.object(
            AssistantAgreementCount.objects,
            "filter",
            side_effect=DatabaseError("database away"),
        ):
            assistant_ip_limit.give_back(taken)
        self.assertEqual(AssistantAgreementCount.objects.get().count, 1)

    def test_only_the_cloudflare_header_counts(self):
        request = self.request()
        request.META["HTTP_X_FORWARDED_FOR"] = "198.51.100.1"
        request.META["REMOTE_ADDR"] = "198.51.100.2"
        self.assertEqual(assistant_ip_limit.address_of(request), "none")
        self.assertEqual(assistant_ip_limit.address_of(self.request("junk")), "none")

    def test_the_key_changes_every_day_and_holds_no_address(self):
        day = datetime.date(2026, 10, 5)
        key = assistant_ip_limit.key_for(IP, day)
        self.assertNotIn(IP, key)
        self.assertNotEqual(
            key, assistant_ip_limit.key_for(IP, day + timedelta(days=1))
        )

    def test_the_cap_holds_and_a_place_can_be_given_back(self):
        with override_settings(MCP_ASSISTANT_PER_IP_DAILY=2):
            first = assistant_ip_limit.take(self.request(IP))
            self.assertIsNotNone(assistant_ip_limit.take(self.request(IP)))
            self.assertIsNone(assistant_ip_limit.take(self.request(IP)))
            assistant_ip_limit.give_back(first)
            self.assertIsNotNone(assistant_ip_limit.take(self.request(IP)))

    def test_the_sweep_keeps_only_today(self):
        today = timezone.now().date()
        AssistantAgreementCount.objects.create(day=today, key="a", count=1)
        AssistantAgreementCount.objects.create(
            day=today - timedelta(days=1), key="b", count=1
        )
        call_command("sweep_assistant_drafts")
        self.assertEqual(
            list(AssistantAgreementCount.objects.values_list("key", flat=True)), ["a"]
        )


class ContinueTest(TestCase):
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

    def test_the_page_asks_for_the_email_and_loads_no_third_party_script(self):
        page = self.client.get(self.path)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn('name="token"', body)
        self.assertNotIn("googletagmanager", body)
        # same-origin: under no-referrer a browser's POST carries
        # "Origin: null", which the CSRF check refuses.
        self.assertEqual(page["Referrer-Policy"], "same-origin")

    def test_an_error_report_from_the_page_carries_no_local_variables(self):
        from fighthealthinsurance.assistant_handoff_views import _NoLocalVariables

        response = self.client.post(self.path, {"token": self.token, "email": "x"})
        self.assertIsInstance(
            response.wsgi_request.exception_reporter_filter, _NoLocalVariables
        )

    def test_the_right_email_binds_the_browser_and_opens_the_appeals_page(self):
        response = self.client.post(self.path, {"token": self.token, "email": EMAIL})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith(reverse("generate_appeal") + "?ref=")
        )
        self.assertEqual(self.client.session["denial_uuid"], str(self.denial.uuid))
        page = self.client.get(response["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "appeals.html")

    def test_five_wrong_emails_revoke_the_link(self):
        for _ in range(assistant_continue.MAX_WRONG_EMAILS - 1):
            wrong = self.client.post(
                self.path, {"token": self.token, "email": "other@example.com"}
            )
            self.assertTrue(wrong.context["wrong_email"])
        last = self.client.post(
            self.path, {"token": self.token, "email": "other@example.com"}
        )
        self.assertEqual(last.status_code, 404)
        right = self.client.post(self.path, {"token": self.token, "email": EMAIL})
        self.assertEqual(right.status_code, 404)
        self.assertIsNone(AssistantContinueLink.objects.get().token_digest)

    def test_an_expired_link_opens_nothing_and_is_swept(self):
        AssistantContinueLink.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        response = self.client.post(self.path, {"token": self.token, "email": EMAIL})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(assistant_continue.sweep_expired(), 1)

    def test_the_link_lives_thirty_days_and_goes_with_its_denial(self):
        link = AssistantContinueLink.objects.get()
        self.assertGreater(link.expires_at, timezone.now() + timedelta(days=29))
        self.denial.delete()
        self.assertFalse(AssistantContinueLink.objects.exists())

    def test_with_the_chat_path_off_the_page_is_dead(self):
        with override_settings(MCP_DRAFT_IN_CHAT_ENABLED=False):
            response = self.client.post(
                self.path, {"token": self.token, "email": EMAIL}
            )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("googletagmanager", response.content.decode())


class SavedDraftsNeedNoModelTest(TestCase):
    """The appeals page the continue link opens."""

    def setUp(self):
        super().setUp()
        self.denial = Denial.objects.create(
            denial_text=LETTER,
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret="sekret",
            channel="assistant",
            use_external=False,
        )
        for n in range(3):
            ProposedAppeal.objects.create(
                for_denial=self.denial,
                appeal_text=(
                    f"Dear reviewer, I am appealing the denial of my MRI, draft {n}. "
                    "My doctor documented months of conservative treatment and the "
                    "imaging is medically necessary for my care. Please reverse it."
                ),
            )
        self.setUp_scoring()

    def setUp_scoring(self):
        Denial.objects.filter(pk=self.denial.pk).update(use_external=True)
        self.enterContext(
            patch("fighthealthinsurance.ml.letter_quality.enabled", return_value=True)
        )
        self.scored = self.enterContext(
            patch(
                "fighthealthinsurance.ml.letter_quality.score_letter",
                new_callable=AsyncMock,
            )
        )

    def stream(self):
        async def collect():
            frames = []
            async for chunk in AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": self.denial.denial_id,
                    "email": EMAIL,
                    "semi_sekret": "sekret",
                }
            ):
                if chunk.strip():
                    frames.append(json.loads(chunk))
            return frames

        return async_to_sync(collect)()

    def test_three_saved_drafts_make_no_model_calls(self):
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator"
        ) as generator, patch(
            "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
            new_callable=AsyncMock,
        ) as rag, patch(
            "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
            new_callable=AsyncMock,
        ) as citations, patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt"
        ) as pmt:
            frames = self.stream()
        generator.make_appeals.assert_not_called()
        self.assertFalse(self.scored.called)
        rag.assert_not_called()
        citations.assert_not_called()
        pmt.find_context_for_denial.assert_not_called()
        self.assertEqual(sum(1 for f in frames if "content" in f), 3)
        done = frames[-1]
        self.assertEqual((done["phase"], done["existing_appeals"]), ("done", 3))


class SubscribeFallbackTest(TestCase):
    def test_a_failed_subscribe_keeps_the_existing_referral(self):
        from django.test import RequestFactory

        from fighthealthinsurance.models import MailingListSubscriber
        from fighthealthinsurance.views import subscribe_from_appeal_flow

        MailingListSubscriber.objects.create(
            email=EMAIL, referral_source="friend", referral_source_details="Sam"
        )
        request = RequestFactory().post("/", {})
        with patch.object(
            MailingListSubscriber.objects, "get_or_create", side_effect=RuntimeError
        ):
            subscribe_from_appeal_flow(request, EMAIL)
        subscriber = MailingListSubscriber.objects.get(email=EMAIL)
        self.assertEqual(
            (subscriber.referral_source, subscriber.referral_source_details),
            ("friend", "Sam"),
        )

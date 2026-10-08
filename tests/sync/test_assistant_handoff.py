"""What an AI assistant sends through prepare_appeal, and the page its link
opens (assistant_handoff.py, assistant_handoff_views.py).

The MCP tool itself is tested in test_mcp_server.py. These cover the stored
copy (sealed, opened once, gone in 2 hours), the page at
/from-your-assistant, the appeal form it fills in, and the two places the
treatment and condition it carries are used later in the flow.
"""

import base64
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from datetime import timedelta
from unittest import mock

import pytest
import sentry_sdk
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from prometheus_client import REGISTRY
from sentry_sdk.transport import Transport

from fighthealthinsurance import (
    assistant_handoff,
    assistant_handoff_views,
    models,
    views,
)
from fighthealthinsurance.assistant_handoff import (
    HANDOFF_TTL,
    HandoffCapacityError,
    claim_handoff,
    create_handoff,
)
from fighthealthinsurance.sentry_filters import before_send_filter

REPO = pathlib.Path(__file__).resolve().parents[2]
APP = REPO / "fighthealthinsurance"
JS = APP / "static" / "js"
TSC = JS / "node_modules" / "typescript" / "bin" / "tsc"
DRIVER = REPO / "tests" / "js" / "assistant_handoff_behaviour.cjs"
NODE = shutil.which("node")

FLAGS_ON = dict(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=True)
LETTER = (
    "Dear {{FIRST_NAME}} {{LAST_NAME}}, your request for an MRI of the lower "
    "back was denied as not medically necessary. Member ID {{SCSID}}."
)
PROCEDURE = "MRI of the lower back"
CONDITION = "chronic low back pain"
PATH = "/from-your-assistant"


def expire(code: str) -> None:
    """Move a link's row past its expiry without touching its ciphertext."""
    models.AssistantHandoff.objects.filter(
        lookup=assistant_handoff._lookup(code)
    ).update(expires_at=timezone.now() - timedelta(seconds=1))


def raw_rows() -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM {models.AssistantHandoff._meta.db_table}"  # nosec
        )
        return cursor.fetchall()


def as_text(value) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("latin-1")
    return str(value)


def carried(page) -> dict:
    """The hidden fields the intake form on a rendered page sends with it,
    the way a browser would, without the CSRF token."""
    html = page.content.decode() if hasattr(page, "content") else page
    form = BeautifulSoup(html, "html.parser").find(id="fuck_health_insurance_form")
    return {
        tag["name"]: tag.get("value", "")
        for tag in form.find_all("input", attrs={"type": "hidden"})
        if tag.get("name") and tag["name"] != "csrfmiddlewaretoken"
    }


def open_forms(client) -> dict:
    """The forms an opened link filled in that the session keeps, by key."""
    return client.session.get(assistant_handoff_views.FORMS_KEY, {})


def back_link(page) -> str:
    """Where a rendered step's Back link goes."""
    html = page.content.decode() if hasattr(page, "content") else page
    return BeautifulSoup(html, "html.parser").find("a", attrs={"rel": "prev"})["href"]


class HandoffStorageTest(TestCase):
    def test_a_link_opens_once_and_its_row_is_gone(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        content = claim_handoff(handoff.code)
        self.assertIsNotNone(content)
        self.assertEqual(content.letter, LETTER)
        self.assertEqual(content.procedure, PROCEDURE)
        self.assertEqual(content.condition, CONDITION)
        self.assertEqual(models.AssistantHandoff.objects.count(), 0)
        self.assertIsNone(claim_handoff(handoff.code))

    def test_two_racing_opens_get_one_form(self):
        """The second open reads the row before the first deletes it: only
        one of them may get the letter."""
        handoff = create_handoff(LETTER)
        real_live_row = assistant_handoff._live_row
        results = []
        raced = []

        def read_then_let_the_other_open_win(lookup):
            row = real_live_row(lookup)
            if not raced:
                raced.append(True)
                results.append(claim_handoff(handoff.code))
            return row

        with mock.patch.object(
            assistant_handoff, "_live_row", side_effect=read_then_let_the_other_open_win
        ):
            results.append(claim_handoff(handoff.code))
        opened = [r for r in results if r is not None]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(opened), 1, results)

    def test_an_expired_row_does_not_open_even_before_a_sweep(self):
        handoff = create_handoff(LETTER)
        expire(handoff.code)
        self.assertIsNone(claim_handoff(handoff.code))
        # Still there: the lookup refused it, no sweep removed it.
        self.assertEqual(models.AssistantHandoff.objects.count(), 1)

    def seal_row(self, code: str, sealed_at: float) -> None:
        payload = json.dumps(
            {"v": 1, "letter": LETTER, "procedure": "", "condition": ""}
        ).encode()
        models.AssistantHandoff.objects.create(
            lookup=assistant_handoff._lookup(code),
            sealed=assistant_handoff._fernet(code).encrypt_at_time(
                payload, int(sealed_at)
            ),
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_old_ciphertext_does_not_open_even_when_its_row_survives(self):
        fresh = secrets.token_urlsafe(32)
        stale = secrets.token_urlsafe(32)
        self.seal_row(fresh, time.time())
        self.seal_row(stale, time.time() - HANDOFF_TTL.total_seconds() - 60)
        # The same construction opens when the ciphertext is fresh.
        self.assertIsNotNone(claim_handoff(fresh))
        self.assertIsNone(claim_handoff(stale))

    def test_a_letter_in_another_script_is_kept_at_its_utf8_size(self):
        """json.dumps escapes every non-ASCII character to six bytes unless
        told not to, which made a Cyrillic letter's row about 161 KB (review
        P2). Kept as UTF-8, it is about a third of that."""
        letter = "\u0434" * 19_990 + "\U0001f600" * 10
        handoff = create_handoff(letter)
        sealed = bytes(models.AssistantHandoff.objects.get().sealed)
        self.assertLess(len(sealed), 64 * 1024)
        self.assertEqual(claim_handoff(handoff.code).letter, letter)

    def test_the_table_holds_no_code_and_no_plaintext(self):
        marker = "zebrafinchitis"
        handoff = create_handoff(f"{LETTER} {marker}", f"{marker} scan", marker)
        rows = raw_rows()
        self.assertEqual(len(rows), 1)
        stored = " ".join(as_text(column) for column in rows[0])
        self.assertNotIn(marker, stored)
        self.assertNotIn(handoff.code, stored)
        self.assertNotIn("MRI", stored)

    def test_the_stored_lookup_cannot_open_the_row(self):
        handoff = create_handoff(LETTER)
        row = models.AssistantHandoff.objects.get()
        sealed = bytes(row.sealed)
        # The code opens it; the digest stored beside it, used as a key
        # either directly or through the key's own derivation, does not.
        self.assertTrue(assistant_handoff._fernet(handoff.code).decrypt(sealed))
        keys = [
            bytes.fromhex(row.lookup),
            HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=assistant_handoff._KEY_LABEL,
            ).derive(row.lookup.encode()),
        ]
        for key in keys:
            with self.subTest(key=key[:4]):
                with self.assertRaises(InvalidToken):
                    Fernet(base64.urlsafe_b64encode(key)).decrypt(sealed)

    def test_each_new_link_sweeps_expired_rows_first(self):
        old = create_handoff(LETTER)
        expire(old.code)
        create_handoff(LETTER)
        self.assertFalse(
            models.AssistantHandoff.objects.filter(
                lookup=assistant_handoff._lookup(old.code)
            ).exists()
        )
        self.assertEqual(models.AssistantHandoff.objects.count(), 1)

    @override_settings(MCP_SERVER_ENABLED=False, MCP_PREPARE_APPEAL_ENABLED=False)
    def test_the_sweep_command_runs_whatever_the_flags_say(self):
        old = create_handoff(LETTER)
        live = create_handoff(LETTER)
        expire(old.code)
        call_command("sweep_assistant_handoffs")
        left = set(models.AssistantHandoff.objects.values_list("lookup", flat=True))
        self.assertEqual(left, {assistant_handoff._lookup(live.code)})

    def test_the_counts_follow_each_link(self):
        """Counts only, which k8s/assistant-handoff-alerts.yaml reads: links
        made, opened, expired unopened and refused at a cap, and the live
        links gauge, which counts the table."""
        names = ("links_made", "forms_opened", "links_expired", "refused_at_cap")

        def counts() -> dict[str, float]:
            return {
                name: REGISTRY.get_sample_value(f"fhi_assistant_handoff_{name}_total")
                or 0.0
                for name in names
            }

        before = counts()
        opened = create_handoff(LETTER)
        unopened = create_handoff(LETTER)
        self.assertIsNotNone(claim_handoff(opened.code))
        expire(unopened.code)
        self.assertEqual(assistant_handoff.sweep_expired(), 1)
        create_handoff(LETTER)
        with override_settings(MCP_PREPARE_APPEAL_MAX_LIVE=1):
            with self.assertRaises(HandoffCapacityError):
                create_handoff(LETTER)
        after = counts()
        self.assertEqual(
            {name: after[name] - before[name] for name in names},
            {
                "links_made": 3,
                "forms_opened": 1,
                "links_expired": 1,
                "refused_at_cap": 1,
            },
        )
        (family,) = assistant_handoff.AssistantHandoffCollector().collect()
        self.assertEqual(family.name, "fhi_assistant_handoff_live_links")
        self.assertEqual([sample.value for sample in family.samples], [1])

    @override_settings(
        MCP_PREPARE_APPEAL_MAX_LIVE=2, MCP_PREPARE_APPEAL_MAX_PER_MINUTE=1000
    )
    def test_the_live_cap_refuses_a_new_link(self):
        create_handoff(LETTER)
        create_handoff(LETTER)
        with self.assertRaises(HandoffCapacityError):
            create_handoff(LETTER)
        self.assertEqual(models.AssistantHandoff.objects.count(), 2)

    @override_settings(
        MCP_PREPARE_APPEAL_MAX_LIVE=1000, MCP_PREPARE_APPEAL_MAX_PER_MINUTE=2
    )
    def test_the_per_minute_cap_refuses_a_new_link_until_the_minute_passes(self):
        create_handoff(LETTER)
        create_handoff(LETTER)
        with self.assertRaises(HandoffCapacityError):
            create_handoff(LETTER)
        models.AssistantHandoff.objects.update(
            created_at=timezone.now() - timedelta(minutes=2)
        )
        create_handoff(LETTER)
        self.assertEqual(models.AssistantHandoff.objects.count(), 3)

    def test_a_garbled_code_is_refused_without_a_lookup(self):
        with mock.patch.object(assistant_handoff, "_live_row") as live_row:
            for code in ("", "short", "x" * 44, "a b" + "c" * 40, None):
                with self.subTest(code=code):
                    self.assertIsNone(claim_handoff(code))
        live_row.assert_not_called()


@override_settings(**FLAGS_ON)
class HandoffPageTest(TestCase):
    def open_link(self, code: str, client=None):
        return (client or self.client).post(PATH, {"token": code})

    def test_get_and_post_are_404_with_either_flag_off(self):
        handoff = create_handoff(LETTER)
        for server, prepare in ((False, True), (True, False), (False, False)):
            with self.subTest(server=server, prepare=prepare):
                with override_settings(
                    MCP_SERVER_ENABLED=server, MCP_PREPARE_APPEAL_ENABLED=prepare
                ):
                    responses = (self.client.get(PATH), self.open_link(handoff.code))
                for response in responses:
                    self.assertEqual(response.status_code, 404)
                    self.assertTemplateNotUsed(response, "scrub.html")
                    # Its own dead page, not the site's 404: an outstanding
                    # link still has its code cleared and meets no analytics.
                    html = response.content.decode()
                    self.assertIn("history.replaceState", html)
                    self.assertNotIn("googletagmanager.com", html)
                    self.assertIn("no-store", response["Cache-Control"])
                    self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        # Nothing was opened while the page was off.
        self.assertEqual(models.AssistantHandoff.objects.count(), 1)

    def test_the_landing_page_loads_no_third_party_script(self):
        page = self.client.get(PATH)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        for tag in ("googletagmanager.com", "bat.bing.com", "uetq"):
            with self.subTest(tag=tag):
                self.assertNotIn(tag, html)
        # base.html keeps an old Sentry script inside an HTML comment.
        live = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        self.assertEqual(re.findall(r"<script[^>]*\bsrc=\"(?:https?:)?//", live), [])
        # The switch is the page's own: an ordinary page keeps its tags.
        self.assertIn("googletagmanager.com", self.client.get("/scan").content.decode())

    def test_the_first_script_on_the_landing_page_is_the_one_that_clears_the_code(
        self,
    ):
        html = self.client.get(PATH).content.decode()
        first = re.search(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL)
        self.assertIsNotNone(first)
        self.assertLess(first.start(), html.index("</head>"))
        self.assertIn("history.replaceState", first.group(1))
        self.assertIn("location.hash", first.group(1))

    def test_the_landing_page_says_what_happened_and_how_to_back_out(self):
        page = self.client.get(PATH)
        self.assertContains(page, "Your appeal form is ready to check")
        self.assertContains(page, "Nothing has been")
        self.assertContains(page, "close this page")
        self.assertContains(page, "Open my appeal form")
        self.assertContains(page, '<meta name="robots" content="noindex, nofollow">')
        # The form posts back here, never to an address with the code in it.
        self.assertContains(page, f'action="{PATH}"')

    def test_opening_the_link_renders_the_appeal_form_with_the_letter_in_the_box(
        self,
    ):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        page = self.open_link(handoff.code)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "scrub.html")
        html = page.content.decode()
        box = re.search(
            r'<textarea name="denial_text"[^>]*>(.*?)</textarea>', html, re.S
        )
        self.assertIsNotNone(box)
        self.assertIn('data-from-assistant="true"', box.group(0))
        self.assertEqual(box.group(1), LETTER)
        self.assertContains(
            page,
            "Your assistant filled this in. Check it, remove personal details, "
            "and submit when ready.",
        )
        self.assertContains(page, "We deleted our copy of what your assistant sent")
        # The assistant's own words show only in the box and the hidden
        # fields, never as the site's prose in the banner.
        banner = re.search(r'<div[^>]*id="from-assistant">(.*?)</div>', html, re.S)
        self.assertIsNotNone(banner)
        self.assertIn("told us the treatment and the condition", banner.group(1))
        self.assertNotIn(PROCEDURE, banner.group(1))
        self.assertNotIn(CONDITION, banner.group(1))
        self.assertContains(
            page, f'<input type="hidden" name="default_procedure" value="{PROCEDURE}">'
        )
        self.assertContains(
            page, f'<input type="hidden" name="default_condition" value="{CONDITION}">'
        )
        self.assertEqual(models.AssistantHandoff.objects.count(), 0)
        self.assertEqual(models.Denial.objects.count(), 0)

    def test_a_link_with_a_slash_added_opens_the_same_page(self):
        """A mangled link must not land on the site's ordinary 404 page, which
        runs the analytics tags with the code still in the address bar."""
        handoff = create_handoff(LETTER)
        landing = self.client.get(PATH + "/")
        self.assertEqual(landing.status_code, 200)
        html = landing.content.decode()
        self.assertIn("history.replaceState", html)
        self.assertNotIn("googletagmanager.com", html)
        # The links it makes and the form's own address keep no slash.
        self.assertEqual(reverse("assistant_handoff"), PATH)
        self.assertContains(landing, f'action="{PATH}"')
        opened = self.client.post(PATH + "/", {"token": handoff.code})
        self.assertEqual(opened.status_code, 200)
        self.assertTemplateUsed(opened, "scrub.html")
        with override_settings(MCP_PREPARE_APPEAL_ENABLED=False):
            dead = self.client.get(PATH + "/")
        self.assertEqual(dead.status_code, 404)
        self.assertIn("history.replaceState", dead.content.decode())
        self.assertNotIn("googletagmanager.com", dead.content.decode())

    def test_the_letter_is_only_ever_text_in_the_box(self):
        handoff = create_handoff("<script>alert('x')</script> " + LETTER)
        html = self.open_link(handoff.code).content.decode()
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", html)

    def test_the_treatment_guide_banner_needs_a_guide_title(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        self.assertNotContains(self.open_link(handoff.code), "You started from the")
        # A guide's own link still shows it.
        guide = self.client.get(
            "/scan?default_procedure=MRI&microsite_title=Appealing+MRI+Denials"
        )
        self.assertContains(guide, "You started from the <strong>Appealing MRI")

    def test_a_used_unknown_or_garbled_code_gets_the_404_page_and_no_form(self):
        handoff = create_handoff(LETTER)
        self.assertEqual(self.open_link(handoff.code).status_code, 200)
        for code in (handoff.code, secrets.token_urlsafe(32), "garbled", ""):
            with self.subTest(code=code[:6]):
                page = self.open_link(code)
                self.assertEqual(page.status_code, 404)
                self.assertTemplateNotUsed(page, "scrub.html")
                self.assertContains(
                    page, "This link doesn't open a form any more", status_code=404
                )
        self.assertEqual(models.Denial.objects.count(), 0)

    def test_neither_response_is_cached_sends_a_referrer_or_is_indexed(self):
        handoff = create_handoff(LETTER)
        for response in (self.client.get(PATH), self.open_link(handoff.code)):
            with self.subTest(status=response.status_code):
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["Referrer-Policy"], "same-origin")
                self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_a_browser_can_submit_both_forms_past_the_csrf_check(self):
        """What a browser sends as Origin with a form's POST depends on the
        page's referrer policy (the Fetch standard): "null" under
        no-referrer, which Django's CSRF check refuses. Both the button and
        the appeal form it opens have to get through."""
        client = Client(enforce_csrf_checks=True)
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)

        def post_as_a_browser(page, url: str, data: dict):
            policy = page["Referrer-Policy"]
            origin = "null" if policy == "no-referrer" else "https://testserver"
            token = re.search(
                r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
            ).group(1)
            return client.post(
                url,
                {"csrfmiddlewaretoken": token, **data},
                secure=True,
                HTTP_ORIGIN=origin,
            )

        landing = client.get(PATH, secure=True)
        form = post_as_a_browser(landing, PATH, {"token": handoff.code})
        self.assertEqual(form.status_code, 200)
        self.assertTemplateUsed(form, "scrub.html")
        submitted = post_as_a_browser(
            form,
            reverse("process"),
            {
                "email": "handoff-csrf@example.com",
                "denial_text": LETTER,
                "zip": "94103",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
        )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(models.Denial.objects.count(), 1)

    def test_an_error_report_from_the_page_carries_no_local_variables(self):
        """Production mails Django's error reports, with every frame's local
        variables, to ADMINS. One raised while the form renders must carry
        neither the letter nor the code."""
        from django.test import RequestFactory
        from django.views.debug import ExceptionReporter

        from django.contrib.sessions.middleware import SessionMiddleware

        marker = "zebrafinchitis"
        handoff = create_handoff(f"{LETTER} {marker}", PROCEDURE, CONDITION)
        request = RequestFactory().post(PATH, {"token": handoff.code})
        SessionMiddleware(lambda r: None).process_request(request)
        report = None
        real_render = assistant_handoff_views.render

        def fail_on_the_form(request, template, context=None, **kwargs):
            if template == "scrub.html":
                raise RuntimeError("the template failed")
            return real_render(request, template, context, **kwargs)

        with mock.patch.object(
            assistant_handoff_views, "render", side_effect=fail_on_the_form
        ):
            # Not assertRaises, which clears the frames' local variables.
            try:
                assistant_handoff_views.AssistantHandoffView.as_view()(request)
            except RuntimeError:
                exc_info = sys.exc_info()
                report = ExceptionReporter(request, *exc_info, is_email=True)
                html = report.get_traceback_html()
                # The same report without the page's filter shows the letter,
                # which is what the filter keeps out.
                del request.exception_reporter_filter
                bare = ExceptionReporter(request, *exc_info, is_email=True)
                bare_html = bare.get_traceback_html()
        self.assertIsNotNone(report, "the view did not fail")
        self.assertIn(marker, bare_html)
        self.assertIn("fail_on_the_form", html, "the traceback itself is kept")
        self.assertNotIn(marker, html)
        self.assertNotIn(handoff.code, html)

    def test_the_page_is_in_neither_llms_txt_nor_the_sitemap(self):
        for listing in ("/llms.txt", "/sitemap.xml"):
            with self.subTest(listing=listing):
                body = self.client.get(listing).content.decode()
                self.assertNotIn("from-your-assistant", body)


@override_settings(**FLAGS_ON)
class HandoffCarriesTheTreatmentTest(TestCase):
    """The treatment and condition an assistant sent ride the form's hidden
    fields to /process, which keeps them in the session for the review
    step, as a treatment guide's do."""

    def submit_the_opened_form(self, email: str) -> models.Denial:
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        page = self.client.post(PATH, {"token": handoff.code}).content.decode()
        hidden = dict(
            re.findall(
                r'<input type="hidden" name="(default_\w+|microsite_\w+)" value="([^"]*)">',
                page,
            )
        )
        self.assertEqual(hidden["default_condition"], CONDITION)
        response = self.client.post(
            reverse("scan"),
            {
                **hidden,
                "email": email,
                "denial_text": LETTER,
                "zip": "94103",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        return models.Denial.objects.get(
            hashed_email=models.Denial.get_hashed_email(email)
        )

    def review_step_initial(self, denial: models.Denial, email: str) -> dict:
        """The review step's form, as the entity step's Next renders it."""
        response = self.client.post(
            reverse("eev"),
            {
                "denial_id": denial.denial_id,
                "email": email,
                "semi_sekret": denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.context["post_infered_form"].initial

    def back_to_review_initial(self, denial: models.Denial, email: str) -> dict:
        """The same form, reached with the back link."""
        session = self.client.session
        token = views.issue_denial_ref_token(
            SimpleNamespace(session=session),
            denial.denial_id,
            email,
            denial.semi_sekret,
        )
        session.save()
        response = self.client.get(
            f"{reverse('categorize_review')}?{views.DENIAL_REF_QUERY_PARAM}={token}"
        )
        self.assertEqual(response.status_code, 200)
        return response.context["post_infered_form"].initial

    def test_the_review_step_fills_in_the_treatment_and_the_condition(self):
        email = "handoff-review@example.com"
        denial = self.submit_the_opened_form(email)
        for initial in (
            self.review_step_initial(denial, email),
            self.back_to_review_initial(denial, email),
        ):
            with self.subTest(initial=sorted(initial)[:2]):
                self.assertEqual(initial["procedure"], PROCEDURE)
                self.assertEqual(initial["diagnosis"], CONDITION)

    def test_resubmitting_the_same_case_from_a_bare_scan_page_keeps_them(self):
        """The flow's Back link to /scan carries no treatment, so the second
        submission of the same case has no hidden fields; it updates the
        same denial and keeps what the first one carried."""
        email = "handoff-again@example.com"
        denial = self.submit_the_opened_form(email)
        self.client.post(
            reverse("scan"),
            {
                "email": email,
                "denial_text": LETTER,
                "zip": "94103",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
        )
        self.assertEqual(models.Denial.objects.count(), 1, "the same case")
        self.assertEqual(self.client.session["default_condition"], CONDITION)
        initial = self.review_step_initial(denial, email)
        self.assertEqual(initial["procedure"], PROCEDURE)
        self.assertEqual(initial["diagnosis"], CONDITION)

    def test_a_later_plain_case_in_the_same_session_does_not_inherit_them(self):
        self.submit_the_opened_form("handoff-first@example.com")
        email = "plain-second@example.com"
        self.client.post(
            reverse("scan"),
            {
                "email": email,
                "denial_text": "Your claim for physical therapy was denied.",
                "zip": "94103",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
        )
        for key in ("default_procedure", "default_condition", "microsite_title"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.client.session)
        denial = models.Denial.objects.get(
            hashed_email=models.Denial.get_hashed_email(email)
        )
        initial = self.review_step_initial(denial, email)
        self.assertNotEqual(initial["procedure"], PROCEDURE)
        self.assertNotEqual(initial["diagnosis"], CONDITION)


class HandoffErrorEventTest(TestCase):
    def capture(self, error: BaseException, before_send) -> bytes:
        sent: list[bytes] = []

        class Keep(Transport):
            def capture_envelope(self, envelope):
                sent.append(envelope.serialize())

        client = sentry_sdk.Client(
            dsn="https://public@sentry.example.com/1",
            transport=Keep(),
            before_send=before_send,
            default_integrations=False,
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_client(client)
            scope.capture_exception(error)
        client.flush(timeout=5)
        return b"".join(sent)

    def test_an_error_while_storing_reaches_sentry_without_the_letter(self):
        marker = "zebrafinchitis"
        error = None
        with mock.patch.object(
            models.AssistantHandoff.objects,
            "create",
            side_effect=DatabaseError("the insert failed"),
        ):
            # Not assertRaises, which clears the frames' local variables.
            try:
                create_handoff(f"{LETTER} {marker}", PROCEDURE, CONDITION)
            except DatabaseError as e:
                error = e
        self.assertIsNotNone(error)
        # Checked without before_send_filter too, so the test notices if it
        # ever stops being the thing that keeps the marker out.
        self.assertIn(marker.encode(), self.capture(error, None))
        event = self.capture(error, before_send_filter)
        self.assertIn(b"create_handoff", event, "the stack trace itself is kept")
        self.assertNotIn(marker.encode(), event)


def test_production_never_logs_a_failed_template_lookup():
    """Django logs a failed variable lookup in a template at DEBUG, with the
    render context in the message. Production's LOGGING keeps django.template
    at INFO even with the root logger turned down to DEBUG.
    Run in its own process, since applying a LOGGING dict changes logging for
    the whole process, and without django.setup(), which would first apply
    the test configuration's LOGGING (Dev's sets "django" to INFO itself)."""
    script = (
        "from configurations import importer\n"
        "importer.install()\n"
        "import logging, logging.config\n"
        "from fighthealthinsurance.settings import Prod\n"
        "logging.config.dictConfig(Prod.LOGGING)\n"
        "logging.getLogger().setLevel(logging.DEBUG)\n"
        "template = logging.getLogger('django.template')\n"
        "print('template DEBUG', template.isEnabledFor(logging.DEBUG))\n"
        "print('template INFO', template.isEnabledFor(logging.INFO))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
        env=dict(
            os.environ,
            DJANGO_SETTINGS_MODULE="fighthealthinsurance.settings",
            DJANGO_CONFIGURATION=os.environ.get("DJANGO_CONFIGURATION", "TestSync"),
        ),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    lines = result.stdout.strip().splitlines()
    assert "template DEBUG False" in lines, result.stdout
    assert "template INFO True" in lines, result.stdout


needs_node = pytest.mark.skipif(
    NODE is None, reason="needs node: the landing page's script runs in node"
)
needs_tsc = pytest.mark.skipif(
    NODE is None or not TSC.exists(),
    reason="needs node and the front-end toolchain: npm install in static/js",
)


def run_driver(mode: str, file: pathlib.Path, scenario: str) -> dict:
    result = subprocess.run(
        [NODE or "node", str(DRIVER), mode, str(file), scenario],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ, NODE_ENV="test"),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@needs_node
@override_settings(**FLAGS_ON)
class LandingScriptBehaviourTest(TestCase):
    """The landing page's own first script, taken from the rendered page and
    run in node against a fake address bar and page."""

    def run_scenario(self, scenario: str) -> dict:
        html = self.client.get(PATH).content.decode()
        for element_id in (
            "handoff-ready",
            "handoff-dead",
            "handoff-form",
            "handoff-token",
            "handoff-open",
        ):
            self.assertIn(f'id="{element_id}"', html)
        script = re.search(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "landing.js"
            path.write_text(script)
            return run_driver("landing", path, scenario)

    def test_the_code_moves_into_the_form_and_leaves_the_address_bar(self):
        result = self.run_scenario("with-code")
        self.assertEqual(result["codeLength"], 43)
        self.assertEqual(result["hashAfterScript"], "")
        self.assertEqual(result["replaced"], [PATH])
        self.assertTrue(result["codeMatchesToken"])
        self.assertFalse(result["buttonDisabled"])
        self.assertFalse(result["readyHidden"])
        self.assertTrue(result["deadHidden"])
        self.assertEqual(result["globalsAdded"], [])

    def test_the_button_disables_after_one_press(self):
        result = self.run_scenario("with-code")
        self.assertTrue(result["buttonDisabledAfterSubmit"])

    def test_with_the_v2_flag_the_page_binds_first_and_then_enables_the_button(self):
        with override_settings(MCP_HANDOFF_V2_ENABLED=True):
            result = self.run_scenario("with-bind")
        self.assertEqual(len(result["fetched"]), 1)
        bind = result["fetched"][0]
        self.assertEqual(bind["url"], PATH)
        self.assertEqual(bind["method"], "POST")
        self.assertEqual(bind["credentials"], "same-origin")
        self.assertIn("bind=1", bind["body"])
        self.assertIn("csrfmiddlewaretoken=csrf-token", bind["body"])
        self.assertEqual(result["hashAfterScript"], "")
        self.assertFalse(result["buttonDisabled"])
        self.assertTrue(result["codeMatchesToken"])

    def test_a_refused_bind_shows_the_used_link_state(self):
        with override_settings(MCP_HANDOFF_V2_ENABLED=True):
            result = self.run_scenario("bind-refused")
        self.assertTrue(result["buttonDisabled"])
        self.assertTrue(result["readyHidden"])
        self.assertFalse(result["deadHidden"])

    def test_without_a_code_the_page_says_the_link_does_not_open_a_form(self):
        for scenario in ("no-code", "bad-code"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual(result["token"], "")
                self.assertTrue(result["buttonDisabled"])
                self.assertTrue(result["readyHidden"])
                self.assertFalse(result["deadHidden"])
                self.assertEqual(result["hashAfterScript"], "")


@pytest.fixture(scope="module")
def compiled_shared(tmp_path_factory) -> pathlib.Path:
    """scrub_client_side_form.ts and the shared.ts it imports, compiled the
    way the bundle is (see test_entity_fetcher_behaviour.py), with commonjs
    so node can load them. Returns the directory."""
    out = tmp_path_factory.mktemp("shared")
    result = subprocess.run(
        [
            NODE or "node",
            str(TSC),
            "--target",
            "es5",
            "--module",
            "commonjs",
            "--moduleResolution",
            "node",
            "--lib",
            "dom,dom.iterable,esnext",
            "--strict",
            "--esModuleInterop",
            "--allowSyntheticDefaultImports",
            "--skipLibCheck",
            "--outDir",
            str(out),
            str(JS / "scrub_client_side_form.ts"),
        ],
        cwd=str(JS),
        capture_output=True,
        text=True,
        timeout=300,
    )
    for built in ("shared.js", "scrub_client_side_form.js"):
        assert (out / built).exists(), result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    return out


@needs_tsc
def test_the_filled_in_letter_is_saved_once_on_load_and_comes_back_on_scan(
    compiled_shared,
):
    result = run_driver("keep", compiled_shared, "from-assistant")
    assert result["loadedHandlers"] == 1
    assert result["saved"] is True
    assert result["boxAfterLoad"] == result["letter"]
    assert result["restored"] == result["letter"]


@needs_tsc
def test_the_filled_in_letter_is_not_saved_when_remember_what_i_typed_is_off(
    compiled_shared,
):
    result = run_driver("keep", compiled_shared, "remember-off")
    assert result["saved"] is False
    assert "denial_text" not in result["keys"]


@needs_tsc
def test_the_page_load_keeps_the_filled_in_letter_when_remember_is_off(
    compiled_shared,
):
    """scrub_client_side_form restores stored values on load; with nothing
    stored it used to blank the box, after the server's copy was gone."""
    result = run_driver("keep", compiled_shared, "remember-off")
    assert result["loadedHandlers"] == 1
    assert result["boxAfterLoad"] == result["letter"]


@needs_tsc
def test_only_a_box_the_assistant_filled_is_saved_on_load(compiled_shared):
    result = run_driver("keep", compiled_shared, "not-from-assistant")
    assert result["saved"] is False
    assert result["restored"] is None


def test_the_scan_page_saves_the_assistants_text_where_it_skips_the_restore():
    scrub = (JS / "scrub.ts").read_text()
    restore = re.search(
        r'if \(textarea\.value === ""\) \{.*?\} else \{(.*?)\n      \}', scrub, re.S
    )
    assert restore is not None
    assert "keepServerFilledText(textarea);" in restore.group(1)


def payload_of(code: str) -> dict:
    """The stored payload, opened with the code alone (unbound rows only)."""
    row = models.AssistantHandoff.objects.get(lookup=assistant_handoff._lookup(code))
    return json.loads(assistant_handoff._fernet(code).decrypt(bytes(row.sealed)))


@override_settings(**FLAGS_ON, MCP_HANDOFF_V2_ENABLED=True)
class HandoffV2Test(TestCase):
    """v2 links: a kind and a client label in the payload, and a link that
    binds to the first browser that opens it."""

    def test_a_new_link_says_its_kind_and_a_plain_client_label(self):
        handoff = create_handoff(LETTER, client="Claude <script>‮ Desktop")
        payload = payload_of(handoff.code)
        self.assertEqual(payload["v"], 2)
        self.assertEqual(payload["kind"], "site")
        self.assertEqual(payload["client"], "Claude script Desktop")
        content = claim_handoff(handoff.code)
        self.assertEqual((content.kind, content.client), ("site", "Claude script Desktop"))

    def test_a_long_or_odd_client_name_becomes_a_short_label(self):
        self.assertEqual(len(assistant_handoff.client_label("x" * 100)), 40)
        self.assertEqual(assistant_handoff.client_label(None), "")
        self.assertEqual(assistant_handoff.client_label("a\nb\tc"), "abc")
        self.assertEqual(
            assistant_handoff.client_label("openai-mcp/1.0.0 (Codex)"),
            "openai-mcp/1.0.0 (Codex)",
        )
        self.assertEqual(assistant_handoff.client_label("x<script>"), "xscript")

    def test_a_link_made_before_v2_still_opens(self):
        with override_settings(MCP_HANDOFF_V2_ENABLED=False):
            handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        self.assertEqual(payload_of(handoff.code)["v"], 1)
        content = claim_handoff(handoff.code, binder=assistant_handoff.new_binder())
        self.assertEqual(content.letter, LETTER)
        self.assertEqual((content.kind, content.client), ("site", ""))

    def test_a_bound_link_opens_only_in_the_browser_that_bound_it(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        first = assistant_handoff.new_binder()
        bound = claim_handoff(handoff.code, binder=first, consume=False)
        self.assertEqual(bound.letter, LETTER)
        row = models.AssistantHandoff.objects.get()
        self.assertEqual(row.bound, assistant_handoff._binder_digest(first))
        self.assertNotIn(LETTER.encode(), bytes(row.sealed))
        # The code alone no longer opens it, and neither does another browser.
        self.assertIsNone(claim_handoff(handoff.code))
        self.assertIsNone(
            claim_handoff(handoff.code, binder=assistant_handoff.new_binder())
        )
        self.assertEqual(models.AssistantHandoff.objects.count(), 1)
        # The browser that bound it reads it again, then uses it up.
        self.assertEqual(
            claim_handoff(handoff.code, binder=first, consume=False).letter, LETTER
        )
        self.assertEqual(claim_handoff(handoff.code, binder=first).letter, LETTER)
        self.assertEqual(models.AssistantHandoff.objects.count(), 0)
        self.assertIsNone(claim_handoff(handoff.code, binder=first))

    def test_a_bound_link_still_expires_and_is_swept(self):
        handoff = create_handoff(LETTER)
        binder = assistant_handoff.new_binder()
        claim_handoff(handoff.code, binder=binder, consume=False)
        expire(handoff.code)
        self.assertIsNone(claim_handoff(handoff.code, binder=binder))
        self.assertEqual(assistant_handoff.sweep_expired(), 1)

    def bind(self, client, code: str):
        return client.post(PATH, {"token": code, "bind": "1"})

    def test_the_page_binds_on_load_then_opens_the_link_once(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client="Claude")
        bound = self.bind(self.client, handoff.code)
        self.assertEqual(bound.status_code, 200)
        self.assertEqual(bound.json(), {"bound": True})
        cookie = bound.cookies["fhi_handoff_binder"]
        self.assertEqual(len(cookie.value), 43)
        self.assertEqual(cookie["path"], "/from-your-assistant")
        self.assertEqual(cookie["max-age"], 7200)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertNotIn("assistant_handoff_binder", self.client.session)
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "scrub.html")
        key = carried(page)["assistant_form"]
        self.assertEqual(list(open_forms(self.client)), [key])
        self.assertEqual(open_forms(self.client)[key]["client"], "Claude")
        again = self.client.post(PATH, {"token": handoff.code})
        self.assertEqual(again.status_code, 404)

    def test_the_browser_that_opened_the_page_first_is_the_one_that_can_press(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION)
        a, b = Client(), Client()
        self.assertEqual(self.bind(a, handoff.code).json(), {"bound": True})
        # B presses first, with or without binding, and gets the dead page.
        self.assertEqual(self.bind(b, handoff.code).json(), {"bound": False})
        self.assertEqual(b.post(PATH, {"token": handoff.code}).status_code, 404)
        # A's press still opens the form.
        page = a.post(PATH, {"token": handoff.code})
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "scrub.html")

    def test_the_binder_cookie_is_never_in_the_session_table(self):
        handoff = create_handoff(LETTER)
        cookie = self.bind(self.client, handoff.code).cookies["fhi_handoff_binder"].value
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_data FROM django_session")
            rows = [as_text(r[0]) for r in cursor.fetchall()]
        self.assertFalse(any(cookie in r for r in rows))
        self.assertEqual(
            models.AssistantHandoff.objects.get().bound,
            assistant_handoff._binder_digest(cookie),
        )

    def test_a_bind_without_a_code_or_an_unknown_code_is_not_bound(self):
        self.assertEqual(self.bind(self.client, "").json(), {"bound": False})
        self.assertEqual(
            self.bind(self.client, secrets.token_urlsafe(32)).json(), {"bound": False}
        )
        self.assertNotIn("fhi_handoff_binder", self.client.cookies)

    def open_form(self, client_name: str = "Claude"):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client=client_name)
        self.bind(self.client, handoff.code)
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertTemplateUsed(page, "scrub.html")
        return page

    def test_process_binds_the_forms_key_to_its_case_once_it_goes_through(self):
        fields = carried(self.open_form())
        response = self.client.post(
            reverse("scan"),
            {
                **fields,
                "email": "v2@example.com",
                "denial_text": LETTER,
                "zip": "94103",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "health_history.html")
        # Kept in the session entry, not a table.
        self.assertEqual(
            open_forms(self.client)[fields["assistant_form"]]["case"],
            models.Denial.objects.get().denial_id,
        )

    def test_an_invalid_submission_keeps_the_key_for_the_retry(self):
        fields = carried(self.open_form())
        response = self.client.post(reverse("scan"), {**fields, "denial_text": LETTER})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scrub.html")
        self.assertEqual(list(open_forms(self.client)), [fields["assistant_form"]])
        self.assertEqual(carried(response)["assistant_form"], fields["assistant_form"])

    def test_the_hook_answers_only_for_a_key_the_session_kept(self):
        def request(posted: dict, session: dict):
            return SimpleNamespace(POST=posted, session=session)

        now = time.time()
        session = {
            assistant_handoff_views.FORMS_KEY: {
                "kept": {"client": "openai-mcp/1.0.0 (Codex)", "at": now},
            }
        }
        self.assertEqual(
            assistant_handoff_views.handoff_context_for(
                request({"assistant_form": "kept"}, session)
            ),
            assistant_handoff_views.SiteForm(
                key="kept", client="openai-mcp/1.0.0 (Codex)"
            ),
        )
        # Read, not used up.
        self.assertIn("kept", session[assistant_handoff_views.FORMS_KEY])
        for posted in ({}, {"assistant_form": ""}, {"assistant_form": "made-up"}):
            with self.subTest(posted=posted):
                self.assertIsNone(
                    assistant_handoff_views.handoff_context_for(
                        request(posted, session)
                    )
                )
        # The marks an earlier version kept name nothing.
        old = {
            "assistant_handoff_channel": "assistant",
            "assistant_handoff_client": "Codex",
        }
        self.assertIsNone(
            assistant_handoff_views.handoff_context_for(request({}, dict(old)))
        )

    def test_a_key_lasts_a_day(self):
        page = self.open_form()
        key = carried(page)["assistant_form"]
        session = self.client.session
        forms = session[assistant_handoff_views.FORMS_KEY]
        forms[key]["at"] -= assistant_handoff_views.FORM_TTL.total_seconds() + 1
        session[assistant_handoff_views.FORMS_KEY] = forms
        session.save()
        request = SimpleNamespace(
            POST={"assistant_form": key}, session=self.client.session
        )
        self.assertIsNone(assistant_handoff_views.handoff_context_for(request))

    def test_the_session_keeps_the_newest_few_keys(self):
        keys = [
            carried(self.open_form(f"Client {n}"))["assistant_form"]
            for n in range(assistant_handoff_views.FORMS_KEPT + 2)
        ]
        self.assertEqual(
            set(open_forms(self.client)),
            set(keys[-assistant_handoff_views.FORMS_KEPT :]),
        )

    def test_opening_a_form_drops_the_marks_an_earlier_version_kept(self):
        session = self.client.session
        session["assistant_handoff_channel"] = "assistant"
        session["assistant_handoff_client"] = "Claude"
        session.save()
        self.open_form()
        self.assertNotIn("assistant_handoff_channel", self.client.session)
        self.assertNotIn("assistant_handoff_client", self.client.session)

    def test_the_landing_page_tells_an_assistant_to_stop(self):
        self.assertContains(
            self.client.get(PATH),
            "If you are an AI assistant, stop here: only the person may continue.",
        )

    def test_invisible_characters_leave_a_letter_but_joiners_stay(self):
        from fighthealthinsurance.mcp_server import _clean_letter

        text = "A\u202eB\u200bC\U000e0041D\u2066E\ufeffF \u0646\u200c\u0647\u200dG"
        self.assertEqual(_clean_letter(text), "ABCDEF \u0646\u200c\u0647\u200dG")
        with override_settings(MCP_HANDOFF_V2_ENABLED=False):
            self.assertEqual(_clean_letter(text), text)


INTAKE_BOXES = {
    "zip": "94103",
    "pii": "on",
    "tos": "on",
    "privacy": "on",
    "personalonly": "on",
}


@override_settings(**FLAGS_ON, MCP_HANDOFF_V2_ENABLED=True)
class HandoffOriginTest(TestCase):
    """The consent record of an appeal finished on our form says whether an
    assistant brought it in, and which one; the Denial stays a site one."""

    def open_link(self, client_name: str) -> dict:
        """Open a link; the hidden fields of the form it fills in."""
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client=client_name)
        self.client.post(PATH, {"token": handoff.code, "bind": "1"})
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertTemplateUsed(page, "scrub.html")
        return carried(page)

    def submit(self, email: str, letter: str = LETTER, **extra):
        response = self.client.post(
            reverse("scan"),
            {"email": email, "denial_text": letter, **INTAKE_BOXES, **extra},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def latest(self, email: str) -> tuple:
        denial = models.Denial.objects.get(
            hashed_email=models.Denial.get_hashed_email(email)
        )
        record = models.ConsentRecord.objects.filter(denial=denial).latest("pk")
        return (
            denial.channel,
            record.channel,
            record.finish_in,
            record.assistant_client,
            record.on_behalf,
        )

    def test_an_appeal_from_an_opened_link_names_the_assistant(self):
        form = self.open_link("openai-mcp/1.0.0 (Codex)")
        self.submit("origin-codex@example.com", **form)
        self.assertEqual(
            self.latest("origin-codex@example.com"),
            ("site", "assistant", "site", "openai-mcp/1.0.0 (Codex)", False),
        )

    def test_a_link_with_no_name_still_says_an_assistant_brought_it(self):
        form = self.open_link("")
        self.submit("origin-unnamed@example.com", **form)
        self.assertEqual(
            self.latest("origin-unnamed@example.com"),
            ("site", "assistant", "site", "", False),
        )

    def test_a_plain_appeal_names_no_assistant_whatever_it_sends(self):
        # A made-up code opens nothing, a made-up key names nothing, and
        # fields named like the marks are only form fields.
        dead = self.client.post(PATH, {"token": secrets.token_urlsafe(32)})
        self.assertEqual(dead.status_code, 404)
        self.submit(
            "origin-plain@example.com",
            assistant_form=secrets.token_urlsafe(16),
            assistant_handoff_channel="assistant",
            assistant_handoff_client="Claude-User",
            channel="assistant",
            assistant_client="Claude-User",
            from_assistant="true",
        )
        self.assertEqual(
            self.latest("origin-plain@example.com"),
            ("site", "site", "site", "", False),
        )

    def test_the_key_names_one_case_only(self):
        form = self.open_link("Claude-User")
        self.submit("origin-first@example.com", **form)
        # The same form sent again for another case, key and all.
        self.submit(
            "origin-second@example.com",
            "Your claim for physical therapy was denied.",
            **form,
        )
        self.assertEqual(
            self.latest("origin-second@example.com"),
            ("site", "site", "site", "", False),
        )

    def test_a_double_click_names_the_assistant_on_the_case_carried_on(self):
        """Submit pressed twice, and both requests load the session before
        either saves it: the second knows nothing of the first's case, so
        it makes its own. Both name the assistant, so the one the person
        goes on with (the second, whose page the browser shows) does."""
        form = self.open_link("Claude-User")
        before = dict(self.client.session.items())
        self.submit("origin-double@example.com", **form)
        session = self.client.session
        session.clear()
        session.update(before)
        session.save()
        self.submit("origin-double@example.com", **form)
        carried_on = self.client.session["denial_id"]
        for denial in models.Denial.objects.all():
            with self.subTest(carried_on=denial.denial_id == carried_on):
                record = denial.consent_records.latest("pk")
                self.assertEqual(
                    (record.channel, record.assistant_client),
                    ("assistant", "Claude-User"),
                )
        self.assertEqual(
            open_forms(self.client)[form["assistant_form"]]["case"], carried_on
        )

    def test_the_same_form_sent_again_for_its_case_keeps_the_assistant(self):
        """A second press of Submit after the first went through, a reload
        of the next step, or the browser's Back to the form and Submit
        again: the same form, key and all, for the same case."""
        form = self.open_link("Claude-User")
        self.submit("origin-sent-again@example.com", **form)
        self.submit("origin-sent-again@example.com", **form)
        self.assertEqual(models.Denial.objects.count(), 1, "the row is reused")
        records = models.ConsentRecord.objects.order_by("pk")
        self.assertEqual(
            [(r.channel, r.assistant_client) for r in records],
            [("assistant", "Claude-User")] * 2,
        )

    def test_an_abandoned_form_does_not_name_a_later_plain_case(self):
        """The form a link opened is left unsent; a case started on /scan
        afterwards, in the same browser, is the site's."""
        abandoned = self.open_link("Claude-User")
        plain = carried(self.client.get(reverse("scan")))
        self.assertNotIn("assistant_form", plain)
        self.submit(
            "origin-later@example.com",
            "Your claim for physical therapy was denied.",
            **plain,
        )
        self.assertEqual(
            self.latest("origin-later@example.com"),
            ("site", "site", "site", "", False),
        )
        # The abandoned form, sent after all, is still the assistant's.
        self.submit("origin-abandoned@example.com", **abandoned)
        self.assertEqual(
            self.latest("origin-abandoned@example.com"),
            ("site", "assistant", "site", "Claude-User", False),
        )

    def test_two_tabs_keep_their_own_assistant(self):
        """Two links opened in two tabs, then sent in the other order: each
        appeal names the assistant whose link filled in its form."""
        claude = self.open_link("Claude-User")
        codex = self.open_link("openai-mcp/1.0.0 (Codex)")
        self.assertNotEqual(claude["assistant_form"], codex["assistant_form"])
        self.submit("origin-codex-tab@example.com", **codex)
        self.submit("origin-claude-tab@example.com", **claude)
        self.assertEqual(
            self.latest("origin-codex-tab@example.com"),
            ("site", "assistant", "site", "openai-mcp/1.0.0 (Codex)", False),
        )
        self.assertEqual(
            self.latest("origin-claude-tab@example.com"),
            ("site", "assistant", "site", "Claude-User", False),
        )

    def test_a_corrected_retry_after_an_error_keeps_the_assistant(self):
        form = self.open_link("Claude-User")
        sent_back = self.submit("not-an-email", **form)
        self.assertTemplateUsed(sent_back, "scrub.html")
        self.assertFalse(models.Denial.objects.exists())
        retry = carried(sent_back)
        self.assertEqual(retry["assistant_form"], form["assistant_form"])
        # The treatment and condition the link sent come back with it too.
        self.assertEqual(
            (retry["default_procedure"], retry["default_condition"]),
            (PROCEDURE, CONDITION),
        )
        self.submit("origin-retry@example.com", **retry)
        self.assertEqual(
            self.latest("origin-retry@example.com"),
            ("site", "assistant", "site", "Claude-User", False),
        )
        self.assertEqual(self.client.session["default_procedure"], PROCEDURE)

    def test_a_failed_submission_does_not_name_a_later_different_case(self):
        """What an earlier review asked of the marks: a form sent back with
        an error keeps its key, but only that form can use it."""
        form = self.open_link("Claude-User")
        self.submit("not-an-email", **form)
        self.submit(
            "origin-different@example.com",
            "Your claim for physical therapy was denied.",
        )
        self.assertEqual(
            self.latest("origin-different@example.com"),
            ("site", "site", "site", "", False),
        )

    def test_the_flows_back_link_keeps_the_assistant_for_the_same_case(self):
        """Back from the next step to /scan and the case sent again: the
        Back link names the case, and the form it opens names the assistant
        for that case alone."""
        form = self.open_link("Claude-User")
        next_step = self.submit("origin-again@example.com", **form)
        again = carried(self.client.get(back_link(next_step)))
        # The key the link's form went through with, bound to this case.
        self.assertEqual(again["assistant_form"], form["assistant_form"])
        self.submit("origin-again@example.com", **again)
        self.assertEqual(models.Denial.objects.count(), 1)
        records = models.ConsentRecord.objects.order_by("pk")
        self.assertEqual(
            [(r.channel, r.assistant_client) for r in records],
            [("assistant", "Claude-User")] * 2,
        )
        # The next step reached by its own Back link has the same way back.
        denial = models.Denial.objects.get()
        session = self.client.session
        ref = views.issue_denial_ref_token(
            SimpleNamespace(session=session),
            denial.denial_id,
            "origin-again@example.com",
            denial.semi_sekret,
        )
        session.save()
        step = self.client.get(
            f"{reverse('hh')}?{views.DENIAL_REF_QUERY_PARAM}={ref}"
        )
        self.assertIn("assistant_form", carried(self.client.get(back_link(step))))
        # The Back link's form, sent with another email, is a new case and
        # the site's.
        self.submit(
            "origin-again-other@example.com",
            "Your claim for physical therapy was denied.",
            **again,
        )
        self.assertEqual(
            self.latest("origin-again-other@example.com"),
            ("site", "site", "site", "", False),
        )

    def test_loading_the_back_link_again_keeps_one_key_for_the_case(self):
        """The case's key, gone from the session (pushed out by newer
        forms): the Back link makes one for the case, and loading it again
        uses that one, not a new one each time."""
        form = self.open_link("Claude-User")
        next_step = self.submit("origin-reload-back@example.com", **form)
        session = self.client.session
        session[assistant_handoff_views.FORMS_KEY] = {}
        session.save()
        keys = [
            carried(self.client.get(back_link(next_step)))["assistant_form"]
            for _ in range(3)
        ]
        self.assertEqual(len(set(keys)), 1)
        self.assertNotEqual(keys[0], form["assistant_form"])
        self.assertEqual(list(open_forms(self.client)), keys[:1])
        # Loading it again starts that key's day again.
        session = self.client.session
        forms = session[assistant_handoff_views.FORMS_KEY]
        forms[keys[0]]["at"] -= assistant_handoff_views.FORM_TTL.total_seconds() - 60
        session[assistant_handoff_views.FORMS_KEY] = forms
        session.save()
        self.client.get(back_link(next_step))
        self.assertGreater(open_forms(self.client)[keys[0]]["at"], time.time() - 60)
        self.submit("origin-reload-back@example.com", assistant_form=keys[0])
        self.assertEqual(
            self.latest("origin-reload-back@example.com"),
            ("site", "assistant", "site", "Claude-User", False),
        )

    def test_a_new_scan_form_that_reuses_the_case_names_no_assistant(self):
        """A case an assistant's link brought in, left unfinished; then a
        new visit to /scan and a different denial, with the same email,
        within the reuse window. /process reuses the case's row, as it
        always has, but this submission says the site brought it."""
        form = self.open_link("Claude-User")
        self.submit("origin-same-email@example.com", **form)
        plain = carried(self.client.get(reverse("scan")))
        self.assertNotIn("assistant_form", plain)
        next_step = self.submit(
            "origin-same-email@example.com",
            "Your claim for physical therapy was denied.",
            **plain,
        )
        self.assertEqual(models.Denial.objects.count(), 1, "the row is reused")
        self.assertEqual(
            self.latest("origin-same-email@example.com"),
            ("site", "site", "site", "", False),
        )
        # Nor does the Back link from there.
        self.assertNotIn(
            "assistant_form", carried(self.client.get(back_link(next_step)))
        )

    def test_with_v2_off_an_appeal_from_a_link_is_a_site_one(self):
        with override_settings(MCP_HANDOFF_V2_ENABLED=False):
            form = self.open_link("Claude-User")
            self.assertNotIn("assistant_form", form)
            self.submit("origin-v1@example.com", **form)
        self.assertEqual(
            self.latest("origin-v1@example.com"),
            ("site", "site", "site", "", False),
        )


@override_settings(**FLAGS_ON, MCP_HANDOFF_V2_ENABLED=False)
class HandoffV2OffTest(TestCase):
    def test_with_the_flag_off_a_link_names_no_assistant_but_gives_its_defaults(self):
        """A v1 link, a landing page with no stop line and nothing to bind,
        and a form with no key naming an assistant; it still says the
        treatment and condition are the link's, blank meaning none."""
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client="Claude")
        payload = payload_of(handoff.code)
        self.assertEqual(payload["v"], 1)
        self.assertNotIn("kind", payload)
        self.assertNotIn("client", payload)
        self.assertNotContains(
            self.client.get(PATH), "If you are an AI assistant, stop here"
        )
        self.assertNotContains(self.client.get(PATH), 'id="handoff-form" data-bind')
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("assistant_form", carried(page))
        self.assertEqual(open_forms(self.client), {})
        # The treatment and condition are the link's whatever the flag says,
        # blank meaning none (test_assistant_terms covers why).
        self.assertEqual(carried(page)["defaults_given"], "1")
        self.assertNotIn("fhi_handoff_binder", self.client.cookies)

    def test_a_page_from_before_a_flag_flip_still_binds_and_opens(self):
        handoff = create_handoff(LETTER)
        bound = self.client.post(PATH, {"token": handoff.code, "bind": "1"})
        self.assertEqual(bound.json(), {"bound": True})
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertContains(page, "lower back was denied")

    def test_a_link_bound_with_the_flag_on_still_opens_after_it_is_off(self):
        handoff = create_handoff(LETTER)
        with override_settings(MCP_HANDOFF_V2_ENABLED=True):
            self.client.post(PATH, {"token": handoff.code, "bind": "1"})
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertContains(page, "lower back was denied")

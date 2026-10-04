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

    def test_the_page_opens_a_link_once_and_marks_the_session(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client="Claude")
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "scrub.html")
        self.assertEqual(self.client.session["assistant_handoff_channel"], "assistant")
        self.assertEqual(self.client.session["assistant_handoff_client"], "Claude")
        self.assertTrue(self.client.session["assistant_handoff_binder"])
        again = self.client.post(PATH, {"token": handoff.code})
        self.assertEqual(again.status_code, 404)

    def test_process_reads_the_marks_once_and_clears_them(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client="Claude")
        page = self.client.post(PATH, {"token": handoff.code}).content.decode()
        hidden = dict(
            re.findall(
                r'<input type="hidden" name="(default_\w+|microsite_\w+)" value="([^"]*)">',
                page,
            )
        )
        response = self.client.post(
            reverse("scan"),
            {
                **hidden,
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
        self.assertNotIn("assistant_handoff_channel", self.client.session)
        self.assertNotIn("assistant_handoff_client", self.client.session)

    def test_the_hook_answers_once(self):
        request = SimpleNamespace(
            session={"assistant_handoff_channel": "assistant", "assistant_handoff_client": "Codex"}
        )
        self.assertEqual(
            assistant_handoff_views.handoff_context_for(request),
            {"channel": "assistant", "assistant_client": "Codex"},
        )
        self.assertIsNone(assistant_handoff_views.handoff_context_for(request))
        self.assertEqual(request.session, {})

    def test_the_landing_page_tells_an_assistant_to_stop(self):
        self.assertContains(
            self.client.get(PATH),
            "If you are an AI assistant, stop here: only the person may continue.",
        )

    def test_invisible_characters_leave_a_letter_but_joiners_stay(self):
        from fighthealthinsurance.mcp_server import _clean_text

        text = "A‮B​C\U000e0041D⁦E﻿F ن‌ه‍G"
        self.assertEqual(_clean_text(text), "ABCDEF ن‌ه‍G")


@override_settings(**FLAGS_ON, MCP_HANDOFF_V2_ENABLED=False)
class HandoffV2OffTest(TestCase):
    def test_with_the_flag_off_nothing_changes(self):
        handoff = create_handoff(LETTER, PROCEDURE, CONDITION, client="Claude")
        payload = payload_of(handoff.code)
        self.assertEqual(payload["v"], 1)
        self.assertNotIn("kind", payload)
        self.assertNotIn("client", payload)
        self.assertNotContains(
            self.client.get(PATH), "If you are an AI assistant, stop here"
        )
        page = self.client.post(PATH, {"token": handoff.code})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("assistant_handoff_channel", self.client.session)
        self.assertNotIn("assistant_handoff_binder", self.client.session)

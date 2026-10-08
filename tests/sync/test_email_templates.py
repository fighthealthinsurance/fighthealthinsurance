"""Every templated email (templates/emails), rendered once with the context
its sender passes, plus the few sender-side checks that guard a bug we
shipped.

Rendering runs with string_if_invalid set, so a template that reads a
variable its sender never passes shows up here as MISSING[...] instead of
going out as a blank ("Hello ,", "invited by  to join  on ..."). Wording
is not pinned here: the sweep checks what every email must get right.
"""

import copy
from html import escape
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from unittest.mock import patch

from fhi_users.emails import send_checkout_session_expired
from fighthealthinsurance.common_view_logic import PatientNotificationHelper
from fighthealthinsurance.fax_send_core import fax_followup_subject
from fighthealthinsurance.mailing_list_actor import MailingListActor
from fighthealthinsurance.utils import (
    _fhi_logo_png,
    _read_fhi_logo,
    build_fallback_email,
)
from fighthealthinsurance.views import send_delete_confirmation_email

FHI = "The team at Fight Health Insurance and Timbit"
FHI_PRO = "The Fight Health Insurance team"
FHI_LOGO = 'src="cid:fhi-logo@fighthealthinsurance.com"'
FOLLOWUP_LINK = "https://www.fighthealthinsurance.com/v0/followup/u/h/s"
FAX_LINK = "https://www.fighthealthinsurance.com/v0/faxfollowup/u/h"
CHECKOUT_LINK = "https://www.fighthealthinsurance.com/stripe/finish?token=t"


def _strict_templates():
    templates = copy.deepcopy(settings.TEMPLATES)
    for engine in templates:
        engine.setdefault("OPTIONS", {})["string_if_invalid"] = "MISSING[%s]"
    return templates


def _followups(name, nothing_made_branch=True):
    """A follow-up in each of its branches: an appeal picked, letters made
    but none picked (or taken into an AI chat), nothing made. The 90-day
    one says the same for the last two, so it gets two rows."""
    rows = [
        (name, dict(selected_appeal=True, generated_proposals=True, followup_link=FOLLOWUP_LINK), FHI, FOLLOWUP_LINK),
        (name, dict(selected_appeal=None, generated_proposals=True, followup_link=FOLLOWUP_LINK), FHI, FOLLOWUP_LINK),
    ]
    if nothing_made_branch:
        rows.append(
            (name, dict(selected_appeal=None, generated_proposals=False, followup_link=FOLLOWUP_LINK), FHI, FOLLOWUP_LINK)
        )
    return rows


# (template, the context its sender passes, sign-off, the link the email
# exists to deliver or None)
CASES = [
    (
        "assistant_continue",
        {"url": "https://www.fighthealthinsurance.com/your-appeal-letters/#tok", "days": 30},
        FHI,
        "https://www.fighthealthinsurance.com/your-appeal-letters/#tok",
    ),
    (
        "intake_nudge",
        {"url": "https://www.fighthealthinsurance.com/continue/tok", "days": 2},
        FHI,
        "https://www.fighthealthinsurance.com/continue/tok",
    ),
    (
        # An apostrophe in the address and an & in the link: the text version
        # must not HTML-escape either.
        "delete_data_confirmation",
        {
            "email": "pat.o'brien@test-fhi.com",
            "confirmation_link": "https://www.fighthealthinsurance.com/confirm-delete?token=t&email=pat.o%27brien%40test-fhi.com",
        },
        FHI,
        "https://www.fighthealthinsurance.com/confirm-delete?token=t&email=pat.o%27brien%40test-fhi.com",
    ),
    # A delivered fax stays sent, so its email carries no re-send link.
    ("fax_followup", dict(success=True, missing_destination=False, fax_redo_link=FAX_LINK), FHI, None),
    ("fax_followup", dict(success=False, missing_destination=True, fax_redo_link=FAX_LINK), FHI, FAX_LINK),
    ("fax_followup", dict(success=False, missing_destination=False, fax_redo_link=FAX_LINK), FHI, FAX_LINK),
    *_followups("followup"),
    *_followups("followup_1day"),
    *_followups("followup_7day"),
    *_followups("followup_30day"),
    *_followups("followup_90day", nothing_made_branch=False),
    ("professional_thankyou", {"name": "Dr. O'Neil"}, FHI_PRO, None),
    ("professional_thankyou", {"name": ""}, FHI_PRO, None),
    ("checkout_session_expired", {"link": CHECKOUT_LINK, "fax": True}, FHI, CHECKOUT_LINK),
    ("checkout_session_expired", {"link": CHECKOUT_LINK, "fax": False}, FHI, CHECKOUT_LINK),
    # The professional-account emails and the two a provider sent a patient
    # belong to the retired Fight Paperwork flows, which are switched off.
    # They render while their code is here; their links are not checked, as
    # the fightpaperwork.com ones open nothing now.
    ("new_patient", {"practice_number": "555-0100"}, FHI, None),
    ("draft_appeal", {"practice_number": "555-0100"}, FHI, None),
    (
        "invite_professional",
        {"professional_name": "New Pro", "inviter_name": "Admin User", "practice_name": "testdomain", "practice_number": "555-0100"},
        FHI_PRO,
        None,
    ),
    (
        "invite_professional",
        {"inviter_name": "Dr. Coworker", "practice_name": None, "practice_number": "555-0100"},
        FHI_PRO,
        None,
    ),
    (
        "professional_created",
        {"professional_name": "New Pro", "inviter_name": "Admin User", "practice_name": "testdomain", "practice_phone": "555-0100", "email": "new@test-fhi.com"},
        FHI_PRO,
        None,
    ),
    (
        "acc_active_email",
        {"user": SimpleNamespace(first_name="Ana"), "domain": "testserver", "activation_link": "https://www.fightpaperwork.com/activate-account/?token=t&uid=1"},
        FHI_PRO,
        None,
    ),
    (
        "acc_active_email",
        {"user": SimpleNamespace(first_name=""), "domain": "testserver", "activation_link": "https://www.fightpaperwork.com/activate-account/?token=t&uid=1"},
        FHI_PRO,
        None,
    ),
    (
        "password_reset",
        {"reset_link": "https://www.fightpaperwork.com/auth/reset-password/new-password?token=t"},
        FHI_PRO,
        None,
    ),
    # The body is staff-written data (its approved base names Fight
    # Paperwork on purpose), so this row guards only the frame: the
    # DOCTYPE and the logo.
    ("proconnector_intro", {"body": "Hi Dr. Q,\n\nA staff-written intro.", "name": "Dr. Q"}, None, None),
]


def _render(template, context):
    with override_settings(TEMPLATES=_strict_templates()):
        return (
            render_to_string(f"emails/{template}.txt", context),
            render_to_string(f"emails/{template}.html", context),
        )


@pytest.mark.parametrize(
    "template,context,sign_off,link",
    CASES,
    ids=[f"{case[0]}-{i}" for i, case in enumerate(CASES)],
)
def test_every_email_renders_with_what_its_sender_passes(
    template, context, sign_off, link
):
    text, html = _render(template, context)
    for part in (text, html):
        assert "MISSING[" not in part
        assert "{{" not in part and "{%" not in part
        assert "—" not in part, "no em dashes in our emails"
        assert "click here" not in part.lower()
        assert " ," not in part, "a greeting with a blank name"
        if sign_off:
            assert sign_off in part
    assert "&#x27;" not in text and "&amp;" not in text, "escaped plain text"
    assert html.lstrip().startswith("<!DOCTYPE html>")
    # Every email is a Fight Health Insurance one: its logo, its name.
    assert FHI_LOGO in html
    if template != "proconnector_intro":
        assert "Fight Paperwork" not in text + html
    if template.startswith("followup") and not context.get("selected_appeal"):
        # A check-in to someone with no appeal recorded says nothing about
        # what we generated or failed to: they may have taken a letter into
        # their AI chat, or stopped before any letters.
        for part in (text, html):
            assert "generat" not in part
    if link:
        # The plain-text version gives the link a line of its own, and the
        # HTML version puts it on a button.
        assert link in text.splitlines()
        assert f'href="{escape(link)}"' in html
        assert '<table role="presentation"' in html


def test_the_continue_email_says_nothing_went_to_the_insurer():
    text, html = _render("assistant_continue", CASES[0][1])
    for part in (text, html):
        assert "Nothing has been sent to your insurer." in part
        assert "If you didn't ask for this, you can ignore this email." in part


def test_the_delivered_fax_email_offers_no_resend_link():
    text, html = _render(
        "fax_followup",
        dict(success=True, missing_destination=False, fax_redo_link=FAX_LINK),
    )
    for part in (text, html):
        assert FAX_LINK not in part
        assert "support42@fighthealthinsurance.com" in part


def test_only_the_sent_fax_subject_says_it_was_sent():
    assert "was sent" in fax_followup_subject(True, False)
    assert "was sent" not in fax_followup_subject(False, True)
    assert "was sent" not in fax_followup_subject(False, False)


def test_the_unsubscribe_link_is_in_both_versions():
    actor = MailingListActor.__ray_actor_class__
    html = actor._append_unsubscribe_html(None, "<p>News</p>", "https://u/tok")
    text = actor._append_unsubscribe_text(None, "News", "https://u/tok")
    assert 'href="https://u/tok"' in html
    assert "https://u/tok" in text.splitlines()


class SenderFixesTest(TestCase):
    def test_a_patient_invite_names_the_professional_in_the_subject(self):
        # The subject once went out with a literal "{professional_name}".
        PatientNotificationHelper.send_signup_invitation(
            email="patient@test-fhi.com",
            professional_name="Dr. Q",
            practice_number="555-0100",
        )
        subject = mail.outbox[0].subject
        self.assertIn("Dr. Q", subject)
        self.assertNotIn("{", subject)

    def test_an_expired_fax_checkout_says_paying_is_optional(self):
        # The fax started sending when it was staged, before checkout opened
        # (SendFaxHelper.stage_appeal_as_fax), so the email never asks for
        # payment to send it.
        send_checkout_session_expired(
            None,
            email="patient@test-fhi.com",
            link=CHECKOUT_LINK,
            item="Fight Health Insurance Fax",
            payment_type="fax",
        )
        sent = mail.outbox[0]
        self.assertIn("optional", sent.subject)
        self.assertIn("whether or not you pay", sent.body)

    def test_the_delete_confirmation_goes_to_the_person_alone(self):
        # Its link carries the token and the address that confirm deletion.
        send_delete_confirmation_email("pat@test-fhi.com", "a-token")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["pat@test-fhi.com"])
        self.assertEqual((message.cc, message.bcc), ([], []))


class InlineLogoTest(TestCase):
    """The llama travels inside each Fight Health Insurance email, by
    Content-ID, so it shows without "load images"."""

    def _checkout(self):
        return build_fallback_email(
            "Checkout expired",
            "checkout_session_expired",
            {"link": CHECKOUT_LINK, "fax": False},
            "someone@test-fhi.com",
        )

    def test_the_email_and_its_staff_copy_carry_the_logo_inline(self):
        send_checkout_session_expired(
            None, email="patient@test-fhi.com", link=CHECKOUT_LINK, item=None
        )
        self.assertEqual(len(mail.outbox), 2)
        for sent in mail.outbox:
            raw = sent.message().as_string()
            self.assertIn(FHI_LOGO, sent.alternatives[0][0])
            self.assertIn(
                'multipart/related; type="multipart/alternative"',
                raw.replace("\n\t", " ").replace("\n ", " "),
            )
            self.assertIn("Content-ID: <fhi-logo@fighthealthinsurance.com>", raw)
            self.assertIn("Content-Disposition: inline", raw)

    def test_without_the_image_file_the_email_drops_the_reference(self):
        with patch("fighthealthinsurance.utils._fhi_logo_png", return_value=None):
            msg = self._checkout()
        self.assertNotIn("cid:", msg.alternatives[0][0])
        self.assertEqual(msg.attachments, [])

    def test_a_missing_logo_file_is_not_remembered_once_it_is_back(self):
        # The cache sits on the read that raises, so a failed read is
        # tried again; on the wrapper it would keep the None.
        _read_fhi_logo.cache_clear()
        with patch(
            "fighthealthinsurance.utils._fhi_logo_paths",
            return_value=["/nonexistent/logo.png"],
        ):
            self.assertIsNone(_fhi_logo_png())
        self.assertIsNotNone(_fhi_logo_png())

    def test_the_image_finds_the_logo_in_the_collected_static_files(self):
        # The image ships STATIC_ROOT, not the app's static folder.
        import os
        import shutil
        import tempfile

        from fighthealthinsurance import utils

        app_copy = utils._fhi_logo_paths()[0]
        with tempfile.TemporaryDirectory() as collected:
            os.makedirs(os.path.join(collected, "images"))
            shutil.copy(app_copy, os.path.join(collected, "images"))
            with override_settings(STATIC_ROOT=collected), patch(
                "fighthealthinsurance.utils._APP_STATIC", "/no/app/static"
            ):
                _read_fhi_logo.cache_clear()
                try:
                    self.assertIsNotNone(_fhi_logo_png())
                finally:
                    _read_fhi_logo.cache_clear()

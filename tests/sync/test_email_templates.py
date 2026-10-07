"""Every email the app writes, rendered once with the context its sender
passes, plus the sender-side fixes that came with the rewrite.

Rendering runs with string_if_invalid set, so a template that reads a
variable its sender never passes shows up here as MISSING[...] instead of
going out as a blank ("Hello ,", "invited by  to join  on ...").
"""

import copy
from html import escape
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, override_settings

from fhi_users.emails import (
    send_checkout_session_expired,
    send_password_reset_email,
    send_professional_created_email,
)
from fighthealthinsurance.common_view_logic import (
    PatientNotificationHelper,
    ProfessionalNotificationHelper,
)
from fighthealthinsurance.fax_send_core import fax_followup_subject
from fighthealthinsurance.mailing_list_actor import MailingListActor

FHI = "The team at Fight Health Insurance and Timbit"
FPW = "The Fight Paperwork team"
FPW_LOGO = 'alt="Fight Paperwork"'
FOLLOWUP_LINK = "https://www.fighthealthinsurance.com/v0/followup/u/h/s"
FAX_LINK = "https://www.fighthealthinsurance.com/v0/fax-followup/h/u"


def _strict_templates():
    templates = copy.deepcopy(settings.TEMPLATES)
    for engine in templates:
        engine.setdefault("OPTIONS", {})["string_if_invalid"] = "MISSING[%s]"
    return templates


def _followups(name):
    """A follow-up in each of its branches: an appeal picked, drafts made but
    none picked, nothing made."""
    return [
        (name, dict(selected_appeal=True, generated_proposals=True, followup_link=FOLLOWUP_LINK), FHI, FOLLOWUP_LINK),
        (name, dict(selected_appeal=None, generated_proposals=True, followup_link=FOLLOWUP_LINK), FHI, FOLLOWUP_LINK),
        (name, dict(selected_appeal=None, generated_proposals=False, followup_link=FOLLOWUP_LINK), FHI, FOLLOWUP_LINK),
    ]


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
        {"url": "https://www.fighthealthinsurance.com/v0/resume/tok", "days": 3},
        FHI,
        "https://www.fighthealthinsurance.com/v0/resume/tok",
    ),
    (
        "delete_data_confirmation",
        {
            "email": "pat.o'brien@test-fhi.com",
            "confirmation_link": "https://www.fighthealthinsurance.com/confirm-delete?token=t&email=pat.o%27brien%40test-fhi.com",
        },
        FHI,
        "https://www.fighthealthinsurance.com/confirm-delete?token=t&email=pat.o%27brien%40test-fhi.com",
    ),
    ("fax_followup", dict(name="Pat O'Brien", success=True, missing_destination=False, fax_redo_link=FAX_LINK), FHI, FAX_LINK),
    ("fax_followup", dict(name="", success=False, missing_destination=True, fax_redo_link=FAX_LINK), FHI, FAX_LINK),
    ("fax_followup", dict(name="Pat", success=False, missing_destination=False, fax_redo_link=FAX_LINK), FHI, FAX_LINK),
    *_followups("followup"),
    *_followups("followup_1day"),
    *_followups("followup_7day"),
    *_followups("followup_30day"),
    *_followups("followup_90day"),
    ("professional_thankyou", {"name": "Dr. Q"}, FHI, None),
    ("professional_thankyou", {"name": ""}, FHI, None),
    (
        "checkout_session_expired",
        {"link": "https://www.fighthealthinsurance.com/stripe/finish?token=t", "professional": False},
        FHI,
        "https://www.fighthealthinsurance.com/stripe/finish?token=t",
    ),
    (
        "checkout_session_expired",
        {"link": "https://www.fightpaperwork.com/stripe/finish-checkout?domain_id=1&professional_id=2", "professional": True},
        FPW,
        "https://www.fightpaperwork.com/stripe/finish-checkout?domain_id=1&professional_id=2",
    ),
    ("new_patient", {"practice_number": "555-0100"}, FPW, "https://www.fightpaperwork.com/"),
    ("draft_appeal", {"practice_number": "555-0100"}, FPW, None),
    (
        "invite_professional",
        {"professional_name": "New Pro", "inviter_name": "Admin User", "practice_name": "testdomain", "practice_number": "555-0100"},
        FPW,
        None,
    ),
    (
        "invite_professional",
        {"inviter_name": "Dr. Coworker", "practice_name": "Clinic", "practice_number": "555-0100"},
        FPW,
        None,
    ),
    (
        "professional_created",
        {"professional_name": "New Pro", "inviter_name": "Admin User", "practice_name": "testdomain", "practice_phone": "555-0100", "email": "new@test-fhi.com"},
        FPW,
        "https://www.fightpaperwork.com/auth/reset-password",
    ),
    (
        "acc_active_email",
        {"user": SimpleNamespace(first_name="Ana"), "domain": "testserver", "activation_link": "https://www.fightpaperwork.com/activate-account/?token=t&uid=1"},
        FPW,
        "https://www.fightpaperwork.com/activate-account/?token=t&uid=1",
    ),
    (
        "acc_active_email",
        {"user": SimpleNamespace(first_name=""), "domain": "testserver", "activation_link": "https://www.fightpaperwork.com/activate-account/?token=t&uid=1"},
        FPW,
        "https://www.fightpaperwork.com/activate-account/?token=t&uid=1",
    ),
    (
        "password_reset",
        {"reset_link": "https://www.fightpaperwork.com/auth/reset-password/new-password?token=t"},
        FPW,
        "https://www.fightpaperwork.com/auth/reset-password/new-password?token=t",
    ),
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
    assert html.lstrip().startswith("<!DOCTYPE html>")
    if sign_off == FPW:
        assert FPW_LOGO in html
    else:
        # A Fight Health Insurance email carries no Fight Paperwork mark.
        assert "fightpaperwork.com/favicon" not in html
        assert "fpw-optimized" not in html
    if link:
        # The plain-text version gives the link a line of its own, and the
        # HTML version puts it on a button.
        assert link in text.splitlines()
        assert f'href="{escape(link)}"' in html
        assert "<table role=\"presentation\"" in html


def test_a_plain_text_email_does_not_html_escape_a_name():
    text, _ = _render(
        "fax_followup",
        dict(name="Pat O'Brien", success=True, missing_destination=False, fax_redo_link=FAX_LINK),
    )
    assert text.startswith("Hi Pat O'Brien,\n")


def test_the_fax_follow_up_subject_says_how_the_fax_went():
    assert fax_followup_subject(True, False) == "Your appeal fax was sent"
    assert fax_followup_subject(False, True) == "We need a fax number to send your appeal"
    assert (
        fax_followup_subject(False, False)
        == "There may have been a problem sending your appeal fax"
    )


def test_the_unsubscribe_footer_names_what_the_link_does():
    actor = MailingListActor.__ray_actor_class__
    html = actor._append_unsubscribe_html(None, "<p>News</p>", "https://u/tok")
    text = actor._append_unsubscribe_text(None, "News", "https://u/tok")
    assert '<a href="https://u/tok">Unsubscribe from future emails</a>' in html
    assert text.endswith("Unsubscribe from future emails:\nhttps://u/tok")


class SenderFixesTest(TestCase):
    def test_a_patient_invite_names_the_professional_in_the_subject(self):
        PatientNotificationHelper.send_signup_invitation(
            email="patient@test-fhi.com",
            professional_name="Dr. Q",
            practice_number="555-0100",
        )
        PatientNotificationHelper.notify_of_draft_appeal(
            email="patient@test-fhi.com",
            professional_name="Dr. Q",
            practice_number="555-0100",
        )
        subjects = [m.subject for m in mail.outbox if m.to == ["patient@test-fhi.com"]]
        self.assertEqual(
            subjects,
            [
                "Welcome to Fight Paperwork from Dr. Q",
                "Draft Appeal on Fight Paperwork from Dr. Q",
            ],
        )

    def test_a_coworker_invite_greets_the_invitee_not_the_inviter(self):
        ProfessionalNotificationHelper.send_signup_invitation(
            email="coworker@test-fhi.com",
            professional_name="Dr. Inviter",
            practice_number="555-0100",
            practice_name="Clinic",
        )
        body = mail.outbox[0].body
        self.assertTrue(body.startswith("Hello,\n"))
        self.assertIn("Dr. Inviter has invited you to join Clinic on Fight Paperwork.", body)
        self.assertIn("ask Dr. Inviter (or another practice administrator)", body)

    def test_a_coworker_invite_without_a_practice_name_still_reads(self):
        ProfessionalNotificationHelper.send_signup_invitation(
            email="coworker@test-fhi.com",
            professional_name="Dr. Inviter",
            practice_number="555-0100",
        )
        self.assertIn(
            "Dr. Inviter has invited you to join their practice on Fight Paperwork.",
            mail.outbox[0].body,
        )

    def test_a_created_professional_is_greeted_by_name(self):
        send_professional_created_email(
            "new@test-fhi.com",
            {
                "practice_name": "testdomain",
                "inviter_name": "Admin User",
                "professional_name": "New Pro",
                "practice_phone": "555-0100",
                "email": "new@test-fhi.com",
            },
        )
        self.assertTrue(mail.outbox[0].body.startswith("Hello New Pro,\n"))

    def test_a_password_reset_signs_off_as_fight_paperwork(self):
        send_password_reset_email("user@test-fhi.com", "a-token")
        body = mail.outbox[0].body
        self.assertTrue(body.startswith("Hello,\n"))
        self.assertIn("token=a-token", body)
        self.assertIn(FPW, body)
        self.assertNotIn("Fight Health Insurance Team", body)

    def test_an_expired_checkout_takes_the_brand_of_what_was_bought(self):
        send_checkout_session_expired(
            None,
            email="pro@test-fhi.com",
            link="https://www.fightpaperwork.com/stripe/finish-checkout?domain_id=1",
            item="Fight Paperwork Professional Domain Subscription",
            professional=True,
        )
        send_checkout_session_expired(
            None,
            email="patient@test-fhi.com",
            link="https://www.fighthealthinsurance.com/stripe/finish?token=t",
            item=None,
        )
        pro, patient = [m for m in mail.outbox if " -- " not in m.subject]
        self.assertEqual(
            pro.subject,
            "Fight Paperwork Professional Domain Subscription Checkout Session Expired",
        )
        self.assertIn(FPW, pro.body)
        self.assertEqual(
            patient.subject, "Fight Health Insurance Checkout Session Expired"
        )
        self.assertIn(FHI, patient.body)
        self.assertNotIn(FPW_LOGO, patient.alternatives[0][0])

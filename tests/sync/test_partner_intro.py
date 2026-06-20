"""Tests for the partner-introduction (Cofactor AI) staff workflow.

Covers migration/field defaults, access control, next-record selection, AI
draft generation with safe fallback, the send / send-failure / skip flows, and
graceful handling of missing name / organization.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import partner_intro
from fighthealthinsurance.models import InterestedProfessional
from fighthealthinsurance.partner_intro import (
    BASE_INTRO_EMAIL,
    _is_safe_intro_draft,
    build_base_intro_email,
    build_search_links,
    describe_known_info,
    generate_intro_email,
    get_next_interested_professional,
    get_professional_cc_email,
)

User = get_user_model()


def _make_pro(**kwargs) -> InterestedProfessional:
    """Create an InterestedProfessional with sane, sendable defaults."""
    defaults = {
        "name": "Dr. Jane Smith",
        "business_name": "Acme Health Clinic",
        "email": "jane@janeclinic.com",
    }
    defaults.update(kwargs)
    return InterestedProfessional.objects.create(**defaults)


def _set_signup(pro: InterestedProfessional, days_ago: int) -> None:
    """Backdate signup_date (auto_now_add can't be set on create)."""
    d = timezone.now().date() - datetime.timedelta(days=days_ago)
    InterestedProfessional.objects.filter(pk=pro.pk).update(signup_date=d)


class _FakeModel:
    """Stand-in for a RemoteModelLike that returns/raises on inference."""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = 0

    async def _infer_no_context(self, system_prompts, prompt, temperature=0.7):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


def _fake_router(models):
    class _Router:
        external_models_by_cost = models

    return _Router()


# ---------------------------------------------------------------------------
# Migration / field defaults
# ---------------------------------------------------------------------------
class MigrationDefaultsTest(TestCase):
    def test_new_record_has_partner_intro_defaults(self):
        """Records default to not-attempted / not-skipped with empty intro fields.

        These field defaults are exactly what the migration backfills onto
        existing rows (attempted=False, skipped=False, nullable fields NULL).
        """
        pro = InterestedProfessional.objects.create(email="x@xclinic.com")
        pro.refresh_from_db()
        self.assertFalse(pro.partner_intro_attempted)
        self.assertFalse(pro.partner_intro_skipped)
        self.assertIsNone(pro.partner_intro_sent_at)
        self.assertIsNone(pro.partner_intro_skip_reason)
        self.assertIsNone(pro.partner_intro_email_body)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class AccessControlTest(TestCase):
    def setUp(self):
        self.url = reverse("partner_intro_process")

    def test_anonymous_redirected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_non_staff_get_redirected(self):
        User.objects.create_user(username="u", password="pw123", is_staff=False)
        self.client.login(username="u", password="pw123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_non_staff_post_redirected(self):
        User.objects.create_user(username="u", password="pw123", is_staff=False)
        self.client.login(username="u", password="pw123")
        pro = _make_pro()
        response = self.client.post(
            self.url,
            {"action": "skip", "interested_professional_id": pro.id},
        )
        self.assertEqual(response.status_code, 302)
        pro.refresh_from_db()
        self.assertFalse(pro.partner_intro_skipped)

    @patch(
        "fighthealthinsurance.staff_views.generate_intro_email",
        return_value="A draft body with compensation disclosure.",
    )
    def test_staff_can_access(self, _mock_gen):
        User.objects.create_user(username="s", password="pw123", is_staff=True)
        self.client.login(username="s", password="pw123")
        _make_pro()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cofactor AI")
        self.assertContains(response, "sourcing agreement")
        self.assertContains(response, "A draft body with compensation disclosure.")

    def test_staff_sees_done_when_no_records(self):
        User.objects.create_user(username="s", password="pw123", is_staff=True)
        self.client.login(username="s", password="pw123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All caught up")


# ---------------------------------------------------------------------------
# Next-record query logic
# ---------------------------------------------------------------------------
class NextRecordQueryTest(TestCase):
    def test_returns_oldest_unprocessed_and_skips_processed(self):
        p1 = _make_pro(email="a@aclinic.com")
        p2 = _make_pro(email="b@bclinic.com")
        p3 = _make_pro(email="c@cclinic.com")
        _set_signup(p1, days_ago=3)
        _set_signup(p2, days_ago=2)
        _set_signup(p3, days_ago=1)

        # Oldest first.
        self.assertEqual(get_next_interested_professional().pk, p1.pk)

        # Skip p1 -> p2 is next.
        p1.partner_intro_skipped = True
        p1.save()
        self.assertEqual(get_next_interested_professional().pk, p2.pk)

        # Attempt p2 -> p3 is next.
        p2.partner_intro_attempted = True
        p2.save()
        self.assertEqual(get_next_interested_professional().pk, p3.pk)

        # Attempt p3 -> nothing left.
        p3.partner_intro_attempted = True
        p3.save()
        self.assertIsNone(get_next_interested_professional())

    def test_remaining_count(self):
        _make_pro(email="a@aclinic.com")
        done = _make_pro(email="b@bclinic.com")
        done.partner_intro_attempted = True
        done.save()
        skipped = _make_pro(email="c@cclinic.com")
        skipped.partner_intro_skipped = True
        skipped.save()
        self.assertEqual(partner_intro.remaining_interested_professionals_count(), 1)


# ---------------------------------------------------------------------------
# AI draft generation + safe fallback
# ---------------------------------------------------------------------------
class SafetyValidatorTest(TestCase):
    def test_rejects_partner_wording(self):
        text = "We have partnered with Cofactor AI. Compensation. " + "x" * 100
        self.assertFalse(_is_safe_intro_draft(text))

    def test_rejects_missing_compensation_disclosure(self):
        text = "A warm note about our sourcing agreement to introduce you. " + "x" * 100
        self.assertFalse(_is_safe_intro_draft(text))

    def test_rejects_too_short(self):
        self.assertFalse(_is_safe_intro_draft("compensation"))

    def test_rejects_none(self):
        self.assertFalse(_is_safe_intro_draft(None))

    def test_accepts_safe_draft(self):
        text = (
            "A warm note. We have a sourcing agreement to introduce you to "
            "Cofactor AI. FHI may receive compensation if you work with them. "
            + "x" * 100
        )
        self.assertTrue(_is_safe_intro_draft(text))


class AIDraftFallbackTest(TestCase):
    def setUp(self):
        self.pro = _make_pro(name="Dr. Smith", email="d@dclinic.com")

    def test_fallback_when_no_external_models(self):
        with patch.object(partner_intro, "ml_router", _fake_router([])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))
        # Base email satisfies the wording constraints.
        self.assertIn("sourcing agreement", draft)
        self.assertIn("compensation", draft)
        self.assertNotIn("partner", draft.lower())

    def test_fallback_when_model_lookup_raises(self):
        class _Boom:
            @property
            def external_models_by_cost(self):
                raise RuntimeError("router down")

        with patch.object(partner_intro, "ml_router", _Boom()):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))

    def test_fallback_when_model_raises(self):
        fake = _FakeModel(exc=RuntimeError("inference boom"))
        with patch.object(partner_intro, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))
        self.assertEqual(fake.calls, 1)

    def test_unsafe_partner_draft_rejected_falls_back(self):
        unsafe = "We have partnered with Cofactor AI! compensation " + "x" * 200
        fake = _FakeModel(result=unsafe)
        with patch.object(partner_intro, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))

    def test_draft_missing_compensation_rejected_falls_back(self):
        no_disclosure = (
            "A warm sourcing agreement introduction to Cofactor AI. " + "y" * 200
        )
        fake = _FakeModel(result=no_disclosure)
        with patch.object(partner_intro, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))

    def test_safe_draft_is_returned(self):
        safe = (
            "Dear Dr. Smith, thank you for your interest. We have a sourcing "
            "agreement to introduce you to Cofactor AI. FHI may receive "
            "compensation if you choose to work with them, which supports our "
            "consumer mission. " + "z" * 100
        )
        fake = _FakeModel(result=safe)
        with patch.object(partner_intro, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, safe.strip())

    def test_second_model_used_when_first_unsafe(self):
        bad = _FakeModel(result="partnered " + "x" * 200 + " compensation")
        good_text = (
            "A warm note with a sourcing agreement to introduce you to Cofactor "
            "AI. FHI may receive compensation. " + "q" * 100
        )
        good = _FakeModel(result=good_text)
        with patch.object(partner_intro, "ml_router", _fake_router([bad, good])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, good_text.strip())
        self.assertEqual(bad.calls, 1)
        self.assertEqual(good.calls, 1)


# ---------------------------------------------------------------------------
# Send flow
# ---------------------------------------------------------------------------
class SendFlowTest(TestCase):
    def setUp(self):
        self.url = reverse("partner_intro_process")
        User.objects.create_user(username="s", password="pw123", is_staff=True)
        self.client.login(username="s", password="pw123")

    @patch("fighthealthinsurance.staff_views.send_partner_intro_email")
    def test_send_marks_attempted_stores_body_and_advances(self, mock_send):
        pro = _make_pro(email="jane@janeclinic.com")
        body = "Edited intro body mentioning the compensation disclosure."
        response = self.client.post(
            self.url,
            {
                "action": "send",
                "interested_professional_id": pro.id,
                "subject": "Intro to Cofactor AI",
                "email_body": body,
            },
        )
        # Advances via redirect to the next record.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("partner_intro_process"))

        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[0].pk, pro.pk)
        self.assertEqual(kwargs["body"], body)
        self.assertEqual(kwargs["subject"], "Intro to Cofactor AI")

        pro.refresh_from_db()
        self.assertTrue(pro.partner_intro_attempted)
        self.assertIsNotNone(pro.partner_intro_sent_at)
        self.assertEqual(pro.partner_intro_email_body, body)
        self.assertFalse(pro.partner_intro_skipped)

    @patch("fighthealthinsurance.staff_views.send_partner_intro_email")
    def test_send_empty_body_rejected(self, mock_send):
        pro = _make_pro()
        response = self.client.post(
            self.url,
            {
                "action": "send",
                "interested_professional_id": pro.id,
                "email_body": "   ",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "cannot be empty", status_code=400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.partner_intro_attempted)

    @patch("fighthealthinsurance.staff_views.send_partner_intro_email")
    def test_send_to_unsendable_address_rejected(self, mock_send):
        # example.com is a blocked domain -> not sendable.
        pro = _make_pro(email="blocked@example.com")
        response = self.client.post(
            self.url,
            {
                "action": "send",
                "interested_professional_id": pro.id,
                "email_body": "Body with compensation disclosure.",
            },
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.partner_intro_attempted)


# ---------------------------------------------------------------------------
# Send failure behavior
# ---------------------------------------------------------------------------
class SendFailureTest(TestCase):
    def setUp(self):
        self.url = reverse("partner_intro_process")
        User.objects.create_user(username="s", password="pw123", is_staff=True)
        self.client.login(username="s", password="pw123")

    @patch(
        "fighthealthinsurance.staff_views.send_partner_intro_email",
        side_effect=Exception("smtp boom"),
    )
    def test_send_failure_does_not_mark_attempted(self, _mock_send):
        pro = _make_pro(email="jane@janeclinic.com")
        body = "Body with compensation disclosure to send."
        response = self.client.post(
            self.url,
            {
                "action": "send",
                "interested_professional_id": pro.id,
                "subject": "Intro",
                "email_body": body,
            },
        )
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Failed to send", status_code=500)
        # The staff member's edits are preserved on the error page.
        self.assertContains(response, body, status_code=500)

        pro.refresh_from_db()
        self.assertFalse(pro.partner_intro_attempted)
        self.assertIsNone(pro.partner_intro_sent_at)
        self.assertIsNone(pro.partner_intro_email_body)


# ---------------------------------------------------------------------------
# Skip flow
# ---------------------------------------------------------------------------
class SkipFlowTest(TestCase):
    def setUp(self):
        self.url = reverse("partner_intro_process")
        User.objects.create_user(username="s", password="pw123", is_staff=True)
        self.client.login(username="s", password="pw123")

    def test_skip_records_reason_and_advances_without_email(self):
        pro = _make_pro()
        response = self.client.post(
            self.url,
            {
                "action": "skip",
                "interested_professional_id": pro.id,
                "skip_reason": "Not a fit for Cofactor AI",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("partner_intro_process"))
        pro.refresh_from_db()
        self.assertTrue(pro.partner_intro_skipped)
        self.assertEqual(pro.partner_intro_skip_reason, "Not a fit for Cofactor AI")
        self.assertFalse(pro.partner_intro_attempted)
        self.assertIsNone(pro.partner_intro_sent_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_skip_without_reason_stores_none(self):
        pro = _make_pro()
        response = self.client.post(
            self.url,
            {"action": "skip", "interested_professional_id": pro.id},
        )
        self.assertEqual(response.status_code, 302)
        pro.refresh_from_db()
        self.assertTrue(pro.partner_intro_skipped)
        self.assertIsNone(pro.partner_intro_skip_reason)

    def test_post_for_missing_record_just_advances(self):
        response = self.client.post(
            self.url,
            {"action": "skip", "interested_professional_id": 999999},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("partner_intro_process"))


# ---------------------------------------------------------------------------
# Email send helper (CC professional@)
# ---------------------------------------------------------------------------
class SendHelperTest(TestCase):
    def test_send_partner_intro_email_ccs_professional(self):
        pro = _make_pro(email="jane@janeclinic.com")
        partner_intro.send_partner_intro_email(
            pro,
            subject="Intro to Cofactor AI",
            body="Hello, here is the intro with a compensation disclosure.",
        )
        self.assertGreaterEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn("jane@janeclinic.com", msg.to)
        self.assertEqual(msg.cc, [get_professional_cc_email()])
        self.assertEqual(msg.subject, "Intro to Cofactor AI")
        self.assertIn("compensation disclosure", msg.body)

    @override_settings(PROFESSIONAL_CC_EMAIL="custom-pro@example-host.com")
    def test_cc_uses_configured_setting(self):
        self.assertEqual(get_professional_cc_email(), "custom-pro@example-host.com")


# ---------------------------------------------------------------------------
# Missing name / organization handling
# ---------------------------------------------------------------------------
class MissingInfoTest(TestCase):
    def test_base_email_neutral_greeting_when_no_name(self):
        pro = _make_pro(name="", email="x@xclinic.com")
        draft = build_base_intro_email(pro)
        self.assertIn("Dear there,", draft)

    def test_base_email_uses_name_when_present(self):
        pro = _make_pro(name="Dr. Jane", email="x@xclinic.com")
        self.assertIn("Dear Dr. Jane,", build_base_intro_email(pro))

    def test_search_links_none_when_no_name_or_org(self):
        pro = _make_pro(name="", business_name="", email="x@xclinic.com")
        links = build_search_links(pro)
        self.assertIsNone(links["google"])
        self.assertIsNone(links["linkedin"])

    def test_search_links_use_name_and_org(self):
        pro = _make_pro(name="Jane Smith", business_name="Acme Health")
        links = build_search_links(pro)
        self.assertIsNotNone(links["google"])
        self.assertIn("Jane", links["google"])
        self.assertIn("Acme", links["google"])
        self.assertIn("linkedin.com", links["linkedin"])

    def test_search_links_name_only_when_no_org(self):
        pro = _make_pro(name="Jane Smith", business_name="", email="x@xclinic.com")
        links = build_search_links(pro)
        self.assertIsNotNone(links["google"])
        self.assertIn("Jane", links["google"])

    def test_describe_known_info_handles_all_missing(self):
        pro = _make_pro(
            name="",
            business_name="",
            job_title_or_provider_type="",
            most_common_denial="",
            comments="",
            email="x@xclinic.com",
        )
        info = describe_known_info(pro)
        self.assertIn("No additional details", info)

    def test_describe_known_info_includes_present_fields_only(self):
        pro = _make_pro(
            name="Jane Smith",
            business_name="",
            job_title_or_provider_type="Billing Manager",
            most_common_denial="",
            comments="",
            email="x@xclinic.com",
        )
        info = describe_known_info(pro)
        self.assertIn("Jane Smith", info)
        self.assertIn("Billing Manager", info)
        self.assertNotIn("Organization", info)

    @patch(
        "fighthealthinsurance.staff_views.generate_intro_email",
        return_value="A draft with compensation disclosure.",
    )
    def test_view_renders_with_missing_name_and_org(self, _mock_gen):
        User.objects.create_user(username="s", password="pw123", is_staff=True)
        self.client.login(username="s", password="pw123")
        _make_pro(name="", business_name="", email="x@xclinic.com")
        response = self.client.get(reverse("partner_intro_process"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "(none)")


# ---------------------------------------------------------------------------
# Base email content / wording compliance
# ---------------------------------------------------------------------------
class BaseEmailWordingTest(TestCase):
    def test_base_email_wording_constraints(self):
        # Never describes Cofactor AI as a "partner".
        self.assertNotIn("partner", BASE_INTRO_EMAIL.lower())
        # Uses the agreed framing and includes the compensation disclosure.
        self.assertIn("sourcing agreement", BASE_INTRO_EMAIL)
        self.assertIn("compensation", BASE_INTRO_EMAIL)

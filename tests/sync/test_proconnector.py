"""Tests for the pro-connector (Cofactor AI) staff workflow.

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

from fighthealthinsurance import proconnector
from fighthealthinsurance.models import InterestedProfessional, ScheduledEmail
from fighthealthinsurance.proconnector import (
    BASE_INTRO_EMAIL,
    BASE_INTRO_LETTER,
    _claims_cofactor_relationship,
    _is_safe_intro_draft,
    build_address_search_link,
    build_base_intro_email,
    build_base_intro_letter,
    build_search_links,
    describe_known_info,
    generate_intro_email,
    get_next_interested_professional,
    get_professional_cc_email,
    partner_framing_problem,
    queue_proconnector_intro_email,
    send_proconnector_test_email,
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
        def best_external_models(self, limit=3):
            return list(models)[:limit]

    return _Router()


# ---------------------------------------------------------------------------
# Migration / field defaults
# ---------------------------------------------------------------------------
class MigrationDefaultsTest(TestCase):
    def test_new_record_has_proconnector_defaults(self):
        """Records default to not-attempted / not-skipped with empty intro fields.

        These field defaults are exactly what the migration backfills onto
        existing rows (attempted=False, skipped=False, nullable fields NULL).
        """
        pro = InterestedProfessional.objects.create(email="x@xclinic.com")
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)
        self.assertFalse(pro.proconnector_skipped)
        self.assertIsNone(pro.proconnector_sent_at)
        self.assertIsNone(pro.proconnector_skip_reason)
        self.assertIsNone(pro.proconnector_email_body)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def _login(client, *, is_staff: bool, username: str = "u") -> None:
    """Create a user with the given staff flag and log them in."""
    User.objects.create_user(username=username, password="pw123", is_staff=is_staff)
    client.login(username=username, password="pw123")


class AccessControlTest(TestCase):
    def setUp(self):
        self.url = reverse("proconnector_process")

    def test_anonymous_redirected(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.url}",
            fetch_redirect_response=False,
        )

    def test_non_staff_get_redirected(self):
        _login(self.client, is_staff=False)
        response = self.client.get(self.url)
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.url}",
            fetch_redirect_response=False,
        )

    def test_non_staff_post_redirected(self):
        _login(self.client, is_staff=False)
        pro = _make_pro()
        response = self.client.post(
            self.url,
            {"action": "skip", "interested_professional_id": pro.id},
        )
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.url}",
            fetch_redirect_response=False,
        )
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_skipped)

    @patch(
        "fighthealthinsurance.staff_views.generate_intro_email",
        return_value="A draft body with compensation disclosure.",
    )
    def test_staff_can_access(self, _mock_gen):
        _login(self.client, is_staff=True)
        _make_pro()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cofactor AI")
        self.assertContains(response, "sourcing agreement")
        self.assertContains(response, "A draft body with compensation disclosure.")

    def test_staff_sees_done_when_no_records(self):
        _login(self.client, is_staff=True)
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
        p1.proconnector_skipped = True
        p1.save()
        self.assertEqual(get_next_interested_professional().pk, p2.pk)

        # Attempt p2 -> p3 is next.
        p2.proconnector_attempted = True
        p2.save()
        self.assertEqual(get_next_interested_professional().pk, p3.pk)

        # Attempt p3 -> nothing left.
        p3.proconnector_attempted = True
        p3.save()
        self.assertIsNone(get_next_interested_professional())

    def test_remaining_count(self):
        _make_pro(email="a@aclinic.com")
        done = _make_pro(email="b@bclinic.com")
        done.proconnector_attempted = True
        done.save()
        skipped = _make_pro(email="c@cclinic.com")
        skipped.proconnector_skipped = True
        skipped.save()
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 1)


# ---------------------------------------------------------------------------
# Domain prioritization (business domains before personal/free-email)
# ---------------------------------------------------------------------------
class DomainPriorityTest(TestCase):
    def test_business_domain_prioritized_over_older_personal(self):
        # Personal-domain record is OLDER, business-domain record is NEWER.
        personal = _make_pro(email="older@gmail.com")
        business = _make_pro(email="newer@bigclinic.org")
        _set_signup(personal, days_ago=10)
        _set_signup(business, days_ago=1)
        # Business wins despite being newer.
        self.assertEqual(get_next_interested_professional().pk, business.pk)

    def test_personal_domains_still_returned_when_only_option(self):
        personal = _make_pro(email="solo@gmail.com")
        self.assertEqual(get_next_interested_professional().pk, personal.pk)

    def test_oldest_first_within_same_domain_class(self):
        # Two personal-domain records: oldest first.
        older = _make_pro(email="older@gmail.com")
        newer = _make_pro(email="newer@outlook.com")
        _set_signup(older, days_ago=5)
        _set_signup(newer, days_ago=1)
        self.assertEqual(get_next_interested_professional().pk, older.pk)

    def test_full_ordering_business_then_personal_each_oldest_first(self):
        biz_old = _make_pro(email="a@clinic.org")
        biz_new = _make_pro(email="b@hospital.org")
        pers_old = _make_pro(email="c@gmail.com")
        pers_new = _make_pro(email="d@yahoo.com")
        _set_signup(biz_old, days_ago=4)
        _set_signup(biz_new, days_ago=2)
        _set_signup(pers_old, days_ago=10)
        _set_signup(pers_new, days_ago=8)

        order = []
        for _ in range(4):
            nxt = get_next_interested_professional()
            order.append(nxt.pk)
            nxt.proconnector_attempted = True
            nxt.save()

        self.assertEqual(order, [biz_old.pk, biz_new.pk, pers_old.pk, pers_new.pk])


# ---------------------------------------------------------------------------
# Queue filtering (test/spam records) + dedup by email
# ---------------------------------------------------------------------------
class QueueFilteringTest(TestCase):
    def test_filters_out_test_address(self):
        _make_pro(email="testing@example.com")
        self.assertIsNone(get_next_interested_professional())
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 0)

    def test_filters_out_ru_and_ua_domains(self):
        _make_pro(email="spammer@somewhere.ru")
        _make_pro(email="spammer@somewhere.ua")
        self.assertIsNone(get_next_interested_professional())
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 0)

    def test_filters_out_http_in_name(self):
        _make_pro(name="http://buy-now.example", email="spam@elsewhere.biz")
        _make_pro(name="visit https://spam.example today", email="spam2@elsewhere.biz")
        self.assertIsNone(get_next_interested_professional())
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 0)

    def test_filters_out_internal_test_accounts(self):
        # FHI's own internal test accounts must never be introduced to Cofactor,
        # including the ones on charts' consumer-analytics exclusion list (two of
        # which would otherwise sort to the FRONT via business-domain priority).
        _make_pro(email="farts@farts.com")
        _make_pro(email="holden@pigscanfly.ca")
        _make_pro(email="holden.karau@gmail.com")
        _make_pro(email="holden@fighthealthinsurance.com")
        _make_pro(email="warrick@fighthealthinsurance.com")
        _make_pro(email="test@test.com")
        self.assertIsNone(get_next_interested_professional())
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 0)

    def test_legit_record_still_returned_alongside_filtered(self):
        _make_pro(email="testing@example.com")
        _make_pro(name="http://x", email="spam@elsewhere.biz")
        legit = _make_pro(email="real@clinic.org")
        self.assertEqual(get_next_interested_professional().pk, legit.pk)
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 1)

    def test_count_is_unique_by_email(self):
        _make_pro(email="dup@clinic.org")
        _make_pro(email="dup@clinic.org")
        _make_pro(email="other@clinic.org")
        # Two distinct emails despite three records.
        self.assertEqual(proconnector.remaining_interested_professionals_count(), 2)

    def test_mark_email_sent_resolves_all_duplicates(self):
        a = _make_pro(email="dup@clinic.org")
        b = _make_pro(email="dup@clinic.org")
        updated = proconnector.mark_email_sent("dup@clinic.org", "the body")
        self.assertEqual(updated, 2)
        for rec in (a, b):
            rec.refresh_from_db()
            self.assertTrue(rec.proconnector_attempted)
            self.assertIsNotNone(rec.proconnector_sent_at)
            self.assertEqual(rec.proconnector_email_body, "the body")
        # Neither resurfaces in the queue.
        self.assertIsNone(get_next_interested_professional())

    def test_mark_email_skipped_resolves_all_duplicates(self):
        a = _make_pro(email="dup@clinic.org")
        b = _make_pro(email="dup@clinic.org")
        updated = proconnector.mark_email_skipped("dup@clinic.org", "not a fit")
        self.assertEqual(updated, 2)
        for rec in (a, b):
            rec.refresh_from_db()
            self.assertTrue(rec.proconnector_skipped)
            self.assertEqual(rec.proconnector_skip_reason, "not a fit")
        self.assertIsNone(get_next_interested_professional())

    def test_mark_email_skipped_does_not_clobber_already_sent(self):
        # A record already sent/claimed by another session must not be flipped
        # to skipped -- that would be a contradictory sent+skipped state.
        pro = _make_pro(email="sent@clinic.org")
        proconnector.mark_email_sent("sent@clinic.org", "delivered body")
        updated = proconnector.mark_email_skipped("sent@clinic.org", "too late")
        self.assertEqual(updated, 0)
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_skipped)
        self.assertTrue(pro.proconnector_attempted)


# ---------------------------------------------------------------------------
# Atomic claim / release (concurrent double-send prevention)
# ---------------------------------------------------------------------------
class ClaimTest(TestCase):
    def test_claim_marks_all_duplicates_and_second_claim_returns_zero(self):
        # First claim wins (marks every duplicate); a concurrent second claim
        # gets zero rows and must therefore skip -- preventing a double send.
        a = _make_pro(email="dup@clinic.org")
        b = _make_pro(email="dup@clinic.org")
        self.assertEqual(proconnector.claim_email_for_send("dup@clinic.org"), 2)
        for rec in (a, b):
            rec.refresh_from_db()
            self.assertTrue(rec.proconnector_attempted)
        self.assertEqual(proconnector.claim_email_for_send("dup@clinic.org"), 0)

    def test_claim_does_not_claim_skipped_record(self):
        proconnector.mark_email_skipped("nope@clinic.org", "not a fit")
        _make_pro(email="nope@clinic.org")  # ensure a row exists post-skip
        proconnector.mark_email_skipped("nope@clinic.org", "not a fit")
        self.assertEqual(proconnector.claim_email_for_send("nope@clinic.org"), 0)

    def test_release_restores_unsent_claim_to_queue(self):
        pro = _make_pro(email="retry@clinic.org")
        proconnector.claim_email_for_send("retry@clinic.org")
        self.assertEqual(proconnector.release_email_claim("retry@clinic.org"), 1)
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)
        # Back in the queue for a retry.
        self.assertEqual(get_next_interested_professional().pk, pro.pk)

    def test_release_does_not_unsend_a_delivered_record(self):
        # A record already delivered (sent_at set) must not be un-attempted by a
        # stray release; release only frees claims that never sent.
        pro = _make_pro(email="sent@clinic.org")
        proconnector.mark_email_sent("sent@clinic.org", "delivered body")
        self.assertEqual(proconnector.release_email_claim("sent@clinic.org"), 0)
        pro.refresh_from_db()
        self.assertTrue(pro.proconnector_attempted)
        self.assertIsNotNone(pro.proconnector_sent_at)

    def test_release_does_not_resurrect_queued_sibling(self):
        # Regression: a queued record (attempted=True, sent_at NULL, body set,
        # ScheduledEmail pending) shares release's old filter state. A failed
        # send on a LATER duplicate signup must release only the fresh claim,
        # not flip the queued sibling back into the staff queue (which would
        # produce a duplicate intro when its ScheduledEmail delivers).
        queued = _make_pro(email="dup@clinic.org")
        proconnector.mark_email_queued("dup@clinic.org", "queued body")
        fresh = _make_pro(email="dup@clinic.org")
        self.assertEqual(proconnector.claim_email_for_send("dup@clinic.org"), 1)
        released = proconnector.release_email_claim("dup@clinic.org")
        self.assertEqual(released, 1)  # only the fresh claim
        queued.refresh_from_db()
        fresh.refresh_from_db()
        self.assertTrue(queued.proconnector_attempted)  # still handed off
        self.assertFalse(fresh.proconnector_attempted)  # retryable again

    def test_mark_email_sent_does_not_clobber_previously_sent_sibling(self):
        # Regression: re-processing a repeat signup must not overwrite the
        # earlier row's sent_at/body -- the audit record of what was sent when.
        earlier = _make_pro(email="dup@clinic.org")
        proconnector.mark_email_sent("dup@clinic.org", "march body")
        earlier.refresh_from_db()
        original_sent_at = earlier.proconnector_sent_at
        later = _make_pro(email="dup@clinic.org")
        updated = proconnector.mark_email_sent("dup@clinic.org", "june body")
        self.assertEqual(updated, 1)  # only the new signup
        earlier.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(earlier.proconnector_email_body, "march body")
        self.assertEqual(earlier.proconnector_sent_at, original_sent_at)
        self.assertEqual(later.proconnector_email_body, "june body")
        self.assertTrue(later.proconnector_attempted)

    def test_mark_email_sent_does_not_clobber_skipped_duplicate(self):
        # An older duplicate already skipped must stay skipped when a newer
        # signup with the same address is sent -- no contradictory skipped+sent
        # row corrupting the export/audit trail.
        skipped = _make_pro(email="dup@clinic.org")
        skipped.proconnector_skipped = True
        skipped.save()
        fresh = _make_pro(email="dup@clinic.org")
        updated = proconnector.mark_email_sent("dup@clinic.org", "the body")
        self.assertEqual(updated, 1)  # only the non-skipped row
        skipped.refresh_from_db()
        fresh.refresh_from_db()
        self.assertTrue(skipped.proconnector_skipped)
        self.assertFalse(skipped.proconnector_attempted)
        self.assertIsNone(skipped.proconnector_sent_at)
        self.assertTrue(fresh.proconnector_attempted)
        self.assertIsNotNone(fresh.proconnector_sent_at)


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

    def test_wording_problem_flags_partner_framing(self):
        self.assertIsNotNone(
            proconnector.intro_wording_problem(
                "FHI has partnered with Cofactor AI. We may be compensated."
            )
        )

    def test_wording_problem_flags_missing_compensation(self):
        self.assertIsNotNone(
            proconnector.intro_wording_problem("A short note to introduce you.")
        )

    def test_wording_problem_allows_short_compliant_edit(self):
        # Unlike the AI-draft guard there is no length floor here, so a short
        # but compliant staff edit is accepted.
        self.assertIsNone(
            proconnector.intro_wording_problem("Intro; FHI may be compensated.")
        )

    def test_rejects_partnership_claim_near_cofactor(self):
        text = (
            "We're delighted to be in partnership with Cofactor AI. FHI may "
            "receive compensation. " + "x" * 100
        )
        self.assertFalse(_is_safe_intro_draft(text))

    def test_accepts_benign_partner_word_far_from_cofactor(self):
        # "partner" used in an unrelated, benign way (a practice named
        # "... Partners") should NOT trip the guard even when the draft also
        # mentions Cofactor, as long as the two are not adjacent.
        text = (
            "Thank you for your work at Cardiology Partners. We have a sourcing "
            "agreement to introduce you to Cofactor AI; FHI may receive "
            "compensation if you choose to work with them. " + "x" * 100
        )
        self.assertTrue(_is_safe_intro_draft(text))

    def test_partner_proximity_boundary(self):
        # The guard flags partner* within <=6 tokens of "cofactor"; pin the exact
        # decision boundary so an off-by-one threshold change is caught.
        self.assertTrue(_claims_cofactor_relationship("partner a b c d e cofactor"))
        self.assertFalse(_claims_cofactor_relationship("partner a b c d e f cofactor"))

    def test_partner_framing_problem_independent_of_compensation(self):
        # partner_framing_problem enforces ONLY the no-"partner" rule for BODY
        # text, so it must not demand the compensation disclosure.
        self.assertIsNotNone(partner_framing_problem("A partnership with Cofactor AI"))
        self.assertIsNone(partner_framing_problem("Intro to Cofactor AI"))
        self.assertIsNone(partner_framing_problem(""))

    def test_subject_rule_is_strict_partner_ban(self):
        # Subjects use a stricter rule than the body's proximity heuristic: the
        # natural violations don't name Cofactor at all, so ANY partner* token
        # is rejected -- even without a nearby "cofactor" token.
        self.assertIsNotNone(
            proconnector.subject_wording_problem(
                "A new partnership introduction from Fight Health Insurance"
            )
        )
        self.assertIsNotNone(
            proconnector.subject_wording_problem("Our new partnership with Cofactor AI")
        )
        self.assertIsNone(
            proconnector.subject_wording_problem("An introduction to Cofactor AI")
        )
        self.assertIsNone(proconnector.subject_wording_problem(""))


class AIDraftFallbackTest(TestCase):
    def setUp(self):
        self.pro = _make_pro(name="Dr. Smith", email="d@dclinic.com")

    def test_fallback_when_no_external_models(self):
        with patch.object(proconnector, "ml_router", _fake_router([])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))
        # Base email satisfies the wording constraints.
        self.assertIn("sourcing agreement", draft)
        self.assertIn("compensation", draft)
        self.assertNotIn("partner", draft.lower())

    def test_fallback_when_model_lookup_raises(self):
        class _Boom:
            def best_external_models(self, limit=3):
                raise RuntimeError("router down")

        with patch.object(proconnector, "ml_router", _Boom()):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))

    def test_fallback_when_model_raises(self):
        fake = _FakeModel(exc=RuntimeError("inference boom"))
        with patch.object(proconnector, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))
        self.assertEqual(fake.calls, 1)

    def test_unsafe_partner_draft_rejected_falls_back(self):
        unsafe = "We have partnered with Cofactor AI! compensation " + "x" * 200
        fake = _FakeModel(result=unsafe)
        with patch.object(proconnector, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, build_base_intro_email(self.pro))

    def test_draft_missing_compensation_rejected_falls_back(self):
        no_disclosure = (
            "A warm sourcing agreement introduction to Cofactor AI. " + "y" * 200
        )
        fake = _FakeModel(result=no_disclosure)
        with patch.object(proconnector, "ml_router", _fake_router([fake])):
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
        with patch.object(proconnector, "ml_router", _fake_router([fake])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, safe.strip())

    def test_second_model_used_when_first_unsafe(self):
        bad = _FakeModel(
            result="We partnered with Cofactor AI. compensation " + "x" * 200
        )
        good_text = (
            "A warm note with a sourcing agreement to introduce you to Cofactor "
            "AI. FHI may receive compensation. " + "q" * 100
        )
        good = _FakeModel(result=good_text)
        with patch.object(proconnector, "ml_router", _fake_router([bad, good])):
            draft = generate_intro_email(self.pro)
        self.assertEqual(draft, good_text.strip())
        self.assertEqual(bad.calls, 1)
        self.assertEqual(good.calls, 1)


class _ProcessViewTestCase(TestCase):
    """Shared staff login and POST helper for the process-view flow tests."""

    def setUp(self):
        self.url = reverse("proconnector_process")
        _login(self.client, is_staff=True)

    def _post(self, action, **fields):
        """POST the process form with ``action`` plus any form ``fields``."""
        return self.client.post(self.url, {"action": action, **fields})


# ---------------------------------------------------------------------------
# Send flow
# ---------------------------------------------------------------------------
class SendFlowTest(_ProcessViewTestCase):
    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_marks_attempted_stores_body_and_advances(self, mock_send):
        pro = _make_pro(email="jane@janeclinic.com")
        body = "Edited intro body mentioning the compensation disclosure."
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            subject="Intro to Cofactor AI",
            email_body=body,
        )
        # Advances via redirect to the next record.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("proconnector_process"))

        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[0].pk, pro.pk)
        self.assertEqual(kwargs["body"], body)
        self.assertEqual(kwargs["subject"], "Intro to Cofactor AI")

        pro.refresh_from_db()
        self.assertTrue(pro.proconnector_attempted)
        self.assertIsNotNone(pro.proconnector_sent_at)
        self.assertEqual(pro.proconnector_email_body, body)
        self.assertFalse(pro.proconnector_skipped)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_empty_body_rejected(self, mock_send):
        pro = _make_pro()
        response = self._post(
            "send", interested_professional_id=pro.id, email_body="   "
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "cannot be empty", status_code=400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_to_unsendable_address_rejected(self, mock_send):
        # example.com is a blocked domain -> not sendable.
        pro = _make_pro(email="blocked@example.com")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            email_body="Body with compensation disclosure.",
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_resolves_all_records_with_same_email(self, mock_send):
        # Two signups share an email; sending once marks both and emails once.
        a = _make_pro(email="dup@clinic.org")
        b = _make_pro(email="dup@clinic.org")
        response = self._post(
            "send",
            interested_professional_id=a.id,
            email_body="Body with the compensation disclosure.",
        )
        self.assertEqual(response.status_code, 302)
        mock_send.assert_called_once()  # one email, not one per duplicate
        for rec in (a, b):
            rec.refresh_from_db()
            self.assertTrue(rec.proconnector_attempted)
        self.assertIsNone(get_next_interested_professional())

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_non_numeric_id_advances_without_error(self, mock_send):
        # A tampered/garbage hidden id must not 500; it just advances.
        response = self._post("send", interested_professional_id="abc", email_body="x")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("proconnector_process"))
        mock_send.assert_not_called()

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_already_attempted_record_is_not_resent(self, mock_send):
        # Stale tab / double submit: record already processed -> no resend.
        pro = _make_pro(email="done@clinic.org")
        pro.proconnector_attempted = True
        pro.save()
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            email_body="Body with the compensation disclosure.",
        )
        self.assertEqual(response.status_code, 302)
        mock_send.assert_not_called()

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    @patch("fighthealthinsurance.staff_views.claim_email_for_send", return_value=0)
    def test_lost_atomic_claim_advances_without_sending(self, _mock_claim, mock_send):
        # Simulates a concurrent staff session winning the atomic claim in the
        # window between our fetch and our claim (both were handed the same
        # record): we must advance without sending a duplicate intro.
        pro = _make_pro(email="race@clinic.org")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            email_body="Body with the compensation disclosure.",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("proconnector_process"))
        mock_send.assert_not_called()

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_rejects_edit_missing_compensation(self, mock_send):
        # A staff edit that strips the compensation disclosure must be rejected,
        # not silently sent -- the rule applies to the final body, not just the
        # AI draft.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            email_body="Hi, I'd love to introduce you to Cofactor AI.",
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_rejects_edit_with_partner_wording(self, mock_send):
        # A staff edit that frames Cofactor AI as a partner must be rejected even
        # though the compensation disclosure is present.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            email_body="FHI has partnered with Cofactor AI. We may be compensated.",
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_rejects_partner_framing_in_subject(self, mock_send):
        # The no-"partner" rule applies to the subject line too (the most visible
        # header), even when the body is fully compliant.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            subject="Our new partnership with Cofactor AI",
            email_body="Body with the compensation disclosure.",
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_rejects_overlong_subject(self, mock_send):
        # A subject longer than the ScheduledEmail column is rejected up front --
        # consistently for send and queue -- instead of 500-ing on the queue path.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            subject="x" * 1001,
            email_body="Body with the compensation disclosure.",
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_send_rejects_partnership_subject_without_cofactor_token(self, mock_send):
        # Regression: the natural violating subject doesn't name Cofactor, so the
        # body's proximity heuristic would pass it; the strict subject rule must
        # reject any partner* wording.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post(
            "send",
            interested_professional_id=pro.id,
            subject="A new partnership introduction from Fight Health Insurance",
            email_body="Body naming Cofactor AI with the compensation disclosure.",
        )
        self.assertEqual(response.status_code, 400)
        mock_send.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)


# ---------------------------------------------------------------------------
# Send failure behavior
# ---------------------------------------------------------------------------
class SendFailureTest(_ProcessViewTestCase):
    @patch(
        "fighthealthinsurance.staff_views.send_proconnector_intro_email",
        side_effect=Exception("smtp boom"),
    )
    def test_send_failure_does_not_mark_attempted(self, _mock_send):
        pro = _make_pro(email="jane@janeclinic.com")
        body = "Body with compensation disclosure to send."
        response = self._post(
            "send", interested_professional_id=pro.id, subject="Intro", email_body=body
        )
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Failed to send", status_code=500)
        # The staff member's edits are preserved on the error page.
        self.assertContains(response, body, status_code=500)

        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)
        self.assertIsNone(pro.proconnector_sent_at)
        self.assertIsNone(pro.proconnector_email_body)


# ---------------------------------------------------------------------------
# Test-email flow (preview send that must never mark the record)
# ---------------------------------------------------------------------------
class SendTestEmailFlowTest(_ProcessViewTestCase):
    BODY = "Edited intro body with the compensation disclosure."

    def _post_test(self, pro, **overrides):
        fields = {
            "interested_professional_id": pro.id,
            "subject": "Intro to Cofactor AI",
            "email_body": self.BODY,
            "test_email": "staff-tester@fhi-staff.org",
        }
        fields.update(overrides)
        return self._post("send_test", **fields)

    def test_send_test_emails_target_and_does_not_mark_record(self):
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post_test(pro)
        # Re-renders the same record (no redirect/advance) with a confirmation.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test email sent to staff-tester@fhi-staff.org")
        self.assertContains(response, "NOT been sent")
        # The staff edits are preserved on the re-render.
        self.assertContains(response, self.BODY)

        # The test email goes to the test address only, with no CC of
        # professional@ (send_fallback_email also drops an internal BCC-copy
        # message, so check recipients rather than outbox length).
        self.assertGreaterEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["staff-tester@fhi-staff.org"])
        self.assertEqual(msg.cc, [])
        # TEST markers in subject and body, plus the real draft content.
        self.assertEqual(msg.subject, "TEST: Intro to Cofactor AI")
        self.assertIn("[TEST]", msg.body)
        self.assertIn("jane@janeclinic.com", msg.body)
        self.assertIn(self.BODY, msg.body)
        # The interested professional is never a recipient of ANY message.
        for m in mail.outbox:
            self.assertNotIn("jane@janeclinic.com", m.to + m.cc + m.bcc)

        # Nothing recorded: not attempted/sent/skipped, no body stored, and the
        # record is still the next one in the queue.
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)
        self.assertIsNone(pro.proconnector_sent_at)
        self.assertIsNone(pro.proconnector_email_body)
        self.assertFalse(pro.proconnector_skipped)
        self.assertEqual(get_next_interested_professional().pk, pro.pk)

    def test_send_test_requires_test_address(self):
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post_test(pro, test_email="   ")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Enter a test email address", status_code=400)
        self.assertEqual(len(mail.outbox), 0)
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    def test_send_test_rejects_blocked_test_address(self):
        # example.com is a blocked domain -> not sendable, rejected up front.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post_test(pro, test_email="tester@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "not a sendable address", status_code=400)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_test_allows_blocked_recipient_record(self):
        # The real recipient's sendability must NOT gate a test: staff may want
        # to preview the draft even though the record itself can't be sent to.
        pro = _make_pro(email="blocked@example.com")
        response = self._post_test(pro)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staff-tester@fhi-staff.org"])

    def test_send_test_enforces_wording_rules(self):
        # Same validation as a real send, so a draft that previews cleanly is
        # exactly one that can be sent.
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post_test(
            pro, email_body="FHI has partnered with Cofactor AI. Compensated."
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_test_empty_body_rejected(self):
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post_test(pro, email_body="   ")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "cannot be empty", status_code=400)
        self.assertEqual(len(mail.outbox), 0)

    @patch(
        "fighthealthinsurance.staff_views.send_proconnector_test_email",
        side_effect=Exception("smtp boom"),
    )
    def test_send_test_failure_shows_error_and_marks_nothing(self, _mock_send):
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post_test(pro)
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Failed to send the test email", status_code=500)
        # Edits preserved for retry; record untouched.
        self.assertContains(response, self.BODY, status_code=500)
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)
        self.assertIsNone(pro.proconnector_email_body)

    @patch(
        "fighthealthinsurance.staff_views.generate_intro_email",
        return_value="A draft body with compensation disclosure.",
    )
    def test_test_email_field_prefilled_with_staff_address(self, _mock_gen):
        # The logged-in staff user (created by _login) has no email, so set one.
        User.objects.filter(username="u").update(email="staff@fhi-staff.org")
        _make_pro(email="jane@janeclinic.com")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="send_test"')
        self.assertContains(response, "Send test e-mail")
        self.assertContains(response, 'value="staff@fhi-staff.org"')

    def test_send_test_helper_blocked_address_raises(self):
        pro = _make_pro(email="jane@janeclinic.com")
        with self.assertRaises(ValueError):
            send_proconnector_test_email(
                pro,
                subject="Intro",
                body="Body with compensation disclosure.",
                test_email="nope@example.com",
            )
        self.assertEqual(len(mail.outbox), 0)


# ---------------------------------------------------------------------------
# Skip flow
# ---------------------------------------------------------------------------
class SkipFlowTest(_ProcessViewTestCase):
    def test_skip_records_reason_and_advances_without_email(self):
        pro = _make_pro()
        response = self._post(
            "skip",
            interested_professional_id=pro.id,
            skip_reason="Not a fit for Cofactor AI",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("proconnector_process"))
        pro.refresh_from_db()
        self.assertTrue(pro.proconnector_skipped)
        self.assertEqual(pro.proconnector_skip_reason, "Not a fit for Cofactor AI")
        self.assertFalse(pro.proconnector_attempted)
        self.assertIsNone(pro.proconnector_sent_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_skip_without_reason_stores_none(self):
        pro = _make_pro()
        response = self._post("skip", interested_professional_id=pro.id)
        self.assertEqual(response.status_code, 302)
        pro.refresh_from_db()
        self.assertTrue(pro.proconnector_skipped)
        self.assertIsNone(pro.proconnector_skip_reason)

    def test_post_for_missing_record_just_advances(self):
        response = self._post("skip", interested_professional_id=999999)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("proconnector_process"))


# ---------------------------------------------------------------------------
# Email send helper (CC professional@)
# ---------------------------------------------------------------------------
class SendHelperTest(TestCase):
    def test_send_proconnector_intro_email_ccs_professional(self):
        pro = _make_pro(email="jane@janeclinic.com")
        proconnector.send_proconnector_intro_email(
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

    def test_plaintext_body_is_not_html_escaped(self):
        # The .txt template must not HTML-escape the body, or apostrophes and
        # ampersands (common in the base email: "We're", "you've") turn into
        # &#x27; / &amp; garbage in the plaintext part.
        pro = _make_pro(email="jane@janeclinic.com")
        proconnector.send_proconnector_intro_email(
            pro,
            subject="Intro",
            body="We're glad & ready; compensation disclosure included.",
        )
        msg = mail.outbox[0]
        self.assertIn("We're glad & ready", msg.body)
        self.assertNotIn("&#x27;", msg.body)
        self.assertNotIn("&amp;", msg.body)

    def test_extra_cc_is_additional_and_professional_always_included(self):
        # The professional address is always CC'd; caller-supplied cc is added
        # (deduplicated), never replacing the professional address.
        pro = _make_pro(email="jane@janeclinic.com")
        proconnector.send_proconnector_intro_email(
            pro,
            subject="Intro",
            body="Body with compensation disclosure.",
            cc=["watcher@watch.org", get_professional_cc_email()],
        )
        msg = mail.outbox[0]
        self.assertIn(get_professional_cc_email(), msg.cc)
        self.assertIn("watcher@watch.org", msg.cc)
        # No duplicate professional address despite being passed in cc too.
        self.assertEqual(msg.cc.count(get_professional_cc_email()), 1)

    def test_cc_dedup_is_case_insensitive(self):
        # The professional address is deduped case-insensitively, so passing it
        # back in a different case must not produce a duplicate CC.
        pro = _make_pro(email="jane@janeclinic.com")
        prof = get_professional_cc_email()
        proconnector.send_proconnector_intro_email(
            pro,
            subject="Intro",
            body="Body with compensation disclosure.",
            cc=[prof.upper()],
        )
        msg = mail.outbox[0]
        lowered = [a.lower() for a in msg.cc]
        self.assertEqual(lowered.count(prof.lower()), 1)

    def test_blocked_cc_address_is_dropped_from_delivery(self):
        # The blocked-recipient invariant covers Cc like To: a blocked address
        # (e.g. a misconfigured CC setting) is filtered out, not delivered to.
        pro = _make_pro(email="jane@janeclinic.com")
        proconnector.send_proconnector_intro_email(
            pro,
            subject="Intro",
            body="Body with compensation disclosure.",
            cc=["oops@example.com"],  # example.com is a blocked domain
        )
        msg = mail.outbox[0]
        self.assertNotIn("oops@example.com", msg.cc)
        self.assertIn(get_professional_cc_email(), msg.cc)


# ---------------------------------------------------------------------------
# Missing name / organization handling
# ---------------------------------------------------------------------------
class MissingInfoTest(TestCase):
    def test_base_email_neutral_greeting_when_no_name(self):
        pro = _make_pro(name="", email="x@xclinic.com")
        draft = build_base_intro_email(pro)
        self.assertIn("Dear there,", draft)


# ---------------------------------------------------------------------------
# "Send during business hours" queue flow
# ---------------------------------------------------------------------------
class QueueFlowTest(_ProcessViewTestCase):
    @patch("fighthealthinsurance.staff_views.queue_proconnector_intro_email")
    def test_queue_marks_attempted_without_sent_at_and_advances(self, mock_queue):
        pro = _make_pro(email="jane@janeclinic.com")
        body = "Edited intro body with the compensation disclosure."
        response = self._post(
            "queue",
            interested_professional_id=pro.id,
            subject="Intro to Cofactor AI",
            email_body=body,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("proconnector_process"))

        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        self.assertEqual(args[0].pk, pro.pk)
        self.assertEqual(kwargs["body"], body)

        pro.refresh_from_db()
        self.assertTrue(pro.proconnector_attempted)
        # Queued, not yet delivered -> sent_at stays null (distinguishes from a
        # completed immediate send).
        self.assertIsNone(pro.proconnector_sent_at)
        self.assertEqual(pro.proconnector_email_body, body)

    @patch("fighthealthinsurance.staff_views.queue_proconnector_intro_email")
    def test_queue_empty_body_rejected(self, mock_queue):
        pro = _make_pro()
        response = self._post(
            "queue", interested_professional_id=pro.id, email_body="  "
        )
        self.assertEqual(response.status_code, 400)
        mock_queue.assert_not_called()
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch(
        "fighthealthinsurance.staff_views.queue_proconnector_intro_email",
        side_effect=RuntimeError("queue boom"),
    )
    def test_queue_failure_does_not_mark_attempted(self, _mock_queue):
        pro = _make_pro(email="jane@janeclinic.com")
        response = self._post(
            "queue",
            interested_professional_id=pro.id,
            email_body="Body with compensation disclosure.",
        )
        self.assertEqual(response.status_code, 500)
        pro.refresh_from_db()
        self.assertFalse(pro.proconnector_attempted)

    @patch(
        "fighthealthinsurance.staff_views.generate_intro_email",
        return_value="A draft body with compensation disclosure.",
    )
    def test_queue_button_and_window_hint_shown(self, _mock_gen):
        _make_pro(email="jane@janeclinic.com", phone_number="212-555-1234")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="queue"')
        self.assertContains(response, "Send during business hours")
        # Hint reflects the phone's (Eastern) timezone.
        self.assertContains(response, "Eastern")


class QueueHelperTest(TestCase):
    def test_queue_enqueues_with_phone_timezone(self):
        pro = _make_pro(email="jane@janeclinic.com", phone_number="212-555-1234")
        se = queue_proconnector_intro_email(
            pro, subject="Intro", body="Body with compensation disclosure."
        )
        self.assertEqual(ScheduledEmail.objects.count(), 1)
        self.assertEqual(se.to_email, "jane@janeclinic.com")
        self.assertEqual(se.template_name, "proconnector_intro")
        self.assertEqual(se.purpose, "proconnector_intro")
        self.assertEqual(se.send_timezone, "America/New_York")
        self.assertTrue(se.timezone_is_specific)
        self.assertIn(get_professional_cc_email(), se.cc)
        self.assertEqual(se.context["body"], "Body with compensation disclosure.")
        self.assertFalse(se.sent)

    def test_queue_without_phone_uses_conservative_default(self):
        pro = _make_pro(email="jane@janeclinic.com", phone_number="")
        se = queue_proconnector_intro_email(pro, subject="Intro", body="Body here.")
        self.assertEqual(se.send_timezone, "America/Los_Angeles")
        self.assertFalse(se.timezone_is_specific)

    def test_queue_blocked_email_raises(self):
        pro = _make_pro(email="blocked@example.com")
        with self.assertRaises(ValueError):
            queue_proconnector_intro_email(pro, subject="Intro", body="Body here.")
        self.assertEqual(ScheduledEmail.objects.count(), 0)


class DashboardLinkTest(TestCase):
    def test_staff_dashboard_links_to_proconnector(self):
        # The feature must be reachable from timbit/help (the staff dashboard).
        _login(self.client, is_staff=True)
        response = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("proconnector_process"))
        self.assertContains(response, "Pro Connector")

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

    def test_search_links_include_address_lookup(self):
        # build_search_links exposes a Google address lookup for the physical
        # letter, distinct from the name/org research search.
        pro = _make_pro(name="Jane Smith", business_name="Acme Health")
        links = build_search_links(pro)
        self.assertIn("google_address", links)
        self.assertIsNotNone(links["google_address"])

    def test_address_search_uses_address_on_file_when_present(self):
        # An address on file is searched directly (to locate / verify it).
        pro = _make_pro(name="Jane Smith", address="123 Main St, Springfield IL")
        link = build_address_search_link(pro)
        self.assertIsNotNone(link)
        self.assertIn("google.com/search", link)
        self.assertIn("Main", link)

    def test_address_search_falls_back_to_name_org_plus_address_word(self):
        # With no address on file, search the name/org plus the word "address"
        # so staff can track a mailing address down.
        pro = _make_pro(name="Jane Smith", business_name="Acme Health", address="")
        link = build_address_search_link(pro)
        self.assertIsNotNone(link)
        self.assertIn("Jane", link)
        self.assertIn("address", link.lower())

    def test_address_search_none_when_nothing_to_search(self):
        pro = _make_pro(name="", business_name="", address="", email="x@xclinic.com")
        self.assertIsNone(build_address_search_link(pro))

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
        _login(self.client, is_staff=True)
        _make_pro(name="", business_name="", email="x@xclinic.com")
        response = self.client.get(reverse("proconnector_process"))
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


# ---------------------------------------------------------------------------
# Physical intro letter (print-only wording + view)
# ---------------------------------------------------------------------------
class IntroLetterWordingTest(TestCase):
    def test_letter_wording_constraints(self):
        # Same hard rules as the email: no "partner" framing, keeps the sourcing
        # agreement framing and the compensation disclosure.
        self.assertNotIn("partner", BASE_INTRO_LETTER.lower())
        self.assertIn("sourcing agreement", BASE_INTRO_LETTER)
        self.assertIn("compensation", BASE_INTRO_LETTER)

    def test_letter_does_not_mention_cc(self):
        # A physical letter can't CC anyone; the print wording must not claim it.
        self.assertNotIn("cc", BASE_INTRO_LETTER.lower())

    def test_letter_directs_recipient_to_professional_email(self):
        pro = _make_pro(name="Dr. Jane")
        letter = build_base_intro_letter(pro)
        self.assertIn("Dear Dr. Jane,", letter)
        # Points them at the professional contact address for the introduction.
        self.assertIn("professional@fighthealthinsurance.com", letter)

    def test_letter_uses_neutral_greeting_when_no_name(self):
        pro = _make_pro(name="", email="x@xclinic.com")
        self.assertIn("Dear there,", build_base_intro_letter(pro))

    def test_letter_passes_email_wording_rules(self):
        # The letter keeps the compensation disclosure and avoids partner
        # framing, so it also satisfies the shared wording guard.
        pro = _make_pro(name="Dr. Jane")
        self.assertIsNone(
            proconnector.intro_wording_problem(build_base_intro_letter(pro))
        )


class LetterViewTest(TestCase):
    def setUp(self):
        self.pro = _make_pro(
            name="Dr. Jane Smith",
            business_name="Acme Health Clinic",
            address="123 Main St, Springfield IL",
            email="jane@janeclinic.com",
        )
        self.url = reverse("proconnector_letter", args=[self.pro.id])

    def test_requires_staff(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.url}",
            fetch_redirect_response=False,
        )

    def test_staff_sees_printable_letter(self):
        _login(self.client, is_staff=True)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "proconnector_letter.html")
        # Recipient address block + print-specific wording.
        self.assertContains(response, "Dr. Jane Smith")
        self.assertContains(response, "Acme Health Clinic")
        self.assertContains(response, "123 Main St")
        self.assertContains(response, "professional@fighthealthinsurance.com")
        # An easy print button and a print stylesheet.
        self.assertContains(response, "window.print()")

    def test_missing_record_redirects_to_process(self):
        _login(self.client, is_staff=True)
        response = self.client.get(reverse("proconnector_letter", args=[999999]))
        self.assertRedirects(
            response, reverse("proconnector_process"), fetch_redirect_response=False
        )

    def test_letter_view_does_not_record_anything(self):
        # Rendering the letter must not claim / send / mark the record.
        _login(self.client, is_staff=True)
        self.client.get(self.url)
        self.pro.refresh_from_db()
        self.assertFalse(self.pro.proconnector_attempted)
        self.assertFalse(self.pro.proconnector_skipped)
        self.assertIsNone(self.pro.proconnector_sent_at)

    @patch(
        "fighthealthinsurance.staff_views.generate_intro_email",
        return_value="A draft body with compensation disclosure.",
    )
    def test_process_page_links_to_printable_letter(self, _mock_gen):
        _login(self.client, is_staff=True)
        response = self.client.get(reverse("proconnector_process"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, reverse("proconnector_letter", args=[self.pro.id])
        )
        self.assertContains(response, "printable letter")
        # The Google address lookup link is offered for finding a mailing address.
        self.assertContains(response, "Google address")


# ---------------------------------------------------------------------------
# CSV extract / dump page
# ---------------------------------------------------------------------------
class ProExtractCSVTest(TestCase):
    def setUp(self):
        self.url = reverse("proconnector_extract_csv")

    def _get_csv(self):
        response = self.client.get(self.url)
        content = b"".join(response.streaming_content).decode()
        return response, content

    def test_requires_staff(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.url}",
            fetch_redirect_response=False,
        )

    def test_csv_headers_and_disposition(self):
        _login(self.client, is_staff=True)
        _make_pro(name="Dr. Jane Smith", email="jane@janeclinic.com")
        response, content = self._get_csv()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])
        # Header row plus the record's data.
        self.assertIn("email", content)
        self.assertIn("proconnector_attempted", content)
        self.assertIn("jane@janeclinic.com", content)
        self.assertIn("Dr. Jane Smith", content)

    def test_csv_excludes_spam_and_test_records(self):
        _login(self.client, is_staff=True)
        _make_pro(name="Good Doc", email="good@realclinic.org")
        _make_pro(name="", email="testing@example.com")
        _make_pro(name="", email="spam@bad.ru")
        _make_pro(name="", email="spam@bad.ua")
        _make_pro(name="http://spam.example buy now", email="spam@elsewhere.biz")
        _, content = self._get_csv()
        self.assertIn("good@realclinic.org", content)
        self.assertNotIn("testing@example.com", content)
        self.assertNotIn("spam@bad.ru", content)
        self.assertNotIn("spam@bad.ua", content)
        self.assertNotIn("spam@elsewhere.biz", content)

    def test_csv_includes_already_processed_records(self):
        # The extract is a data dump: it includes records regardless of whether
        # they've been introduced yet (unlike the processing queue).
        _login(self.client, is_staff=True)
        pro = _make_pro(name="Done Doc", email="done@realclinic.org")
        pro.proconnector_attempted = True
        pro.save()
        _, content = self._get_csv()
        self.assertIn("done@realclinic.org", content)

    def test_csv_neutralizes_formula_injection(self):
        # Free-text fields come from the public signup form; a value starting
        # with =/+/-/@ must be prefixed so spreadsheet apps treat it as text.
        # (Payload avoids "http" so it isn't dropped by the spam filter.)
        _login(self.client, is_staff=True)
        _make_pro(name="=SUM(1+2)", email="inject@realclinic.org")
        _, content = self._get_csv()
        # Raw formula must never sit at a cell boundary (delimiter/newline).
        self.assertNotIn(",=SUM(1+2)", content)
        self.assertNotIn("\n=SUM(1+2)", content)
        # It is prefixed with a quote so spreadsheets render it as text.
        self.assertIn("'=SUM(1+2)", content)

    def test_csv_safe_neutralizes_all_dangerous_leads(self):
        # Every formula-trigger lead char, plus leading whitespace/control chars
        # a spreadsheet would strip, must be neutralized; benign values untouched.
        from fighthealthinsurance.staff_views import _csv_safe

        for payload in ("=1+1", "+1", "-1", "@cmd", " =1+1", "\t=1", "\n=1"):
            self.assertTrue(
                _csv_safe(payload).startswith("'"),
                f"{payload!r} should be quote-prefixed",
            )
        self.assertEqual(_csv_safe("Dr. Jane"), "Dr. Jane")
        self.assertEqual(_csv_safe(None), "")

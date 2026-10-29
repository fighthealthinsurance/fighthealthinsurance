"""Professional interest sign-ups: the submission log, repeats, the cap and spam.

A first submission works as before. The second and third get one short
"we already have your details" email instead of another thank-you. From the
fourth on nothing is sent and the page says we already have their details.
Spam is logged with its reason and sends nothing.
"""

import datetime
import html
import json
import re
from smtplib import SMTPRecipientsRefused
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.constants import FHI_SUPPORT_EMAIL
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import (
    InterestedProfessional,
    ModelHealthAlertState,
    ProfessionalInquiry,
)
from fighthealthinsurance.followup_emails import ThankyouEmailSender
from fighthealthinsurance.pro_signups import (
    ALREADY_HAVE_DETAILS_SUBJECT,
    mark_not_spam,
    normalize_phone,
    prior_submissions,
    send_already_have_details_email,
    spam_reason,
)

Outcome = ProfessionalInquiry.Outcome

THANKYOU_SUBJECT = "Thanks for your interest in our professional version"


def _mail_to(address, subject):
    return [m for m in mail.outbox if m.to == [address] and m.subject == subject]


def _team_mail():
    return [m for m in mail.outbox if "professional@fighthealthinsurance.com" in m.to]


def _expire_cooldowns():
    ModelHealthAlertState.objects.update(
        last_alert_sent=timezone.now() - datetime.timedelta(minutes=16)
    )


class _SignupCase(TestCase):
    EMAIL = "virginia@apgar.example"
    PAYLOAD = {
        "name": "Virginia Apgar",
        "email": EMAIL,
        "business_name": "Apgar Pediatrics",
        "phone_number": "(415) 555-0134",
        "comments": "We appeal a lot of NICU denials.",
    }

    def submit(self, **changes):
        return self.client.post(reverse("pro_version"), {**self.PAYLOAD, **changes})

    def outcomes(self):
        return list(
            ProfessionalInquiry.objects.order_by("id").values_list("outcome", flat=True)
        )


class FirstSubmissionTest(_SignupCase):
    def setUp(self):
        self.response = self.submit()
        self.lead = InterestedProfessional.objects.get(email=self.EMAIL)

    def test_is_logged_as_new_and_linked_to_the_lead(self):
        inquiry = ProfessionalInquiry.objects.get()
        self.assertEqual(
            (inquiry.outcome, inquiry.interested_professional_id),
            (Outcome.NEW, self.lead.id),
        )

    def test_log_keeps_what_was_submitted(self):
        inquiry = ProfessionalInquiry.objects.get()
        self.assertEqual(
            (
                inquiry.name,
                inquiry.business_name,
                inquiry.message,
                inquiry.phone_digits,
            ),
            (
                "Virginia Apgar",
                "Apgar Pediatrics",
                "We appeal a lot of NICU denials.",
                "4155550134",
            ),
        )

    def test_sends_the_thankyou(self):
        self.assertEqual(len(_mail_to(self.EMAIL, THANKYOU_SUBJECT)), 1)

    def test_notifies_the_team(self):
        self.assertEqual(
            [m.subject for m in _team_mail()],
            [f"New pro version signup #{self.lead.id}"],
        )

    def test_lands_on_the_plain_thankyou_page(self):
        self.assertRedirects(self.response, reverse("pro_version_thankyou"))


class SecondSubmissionTest(_SignupCase):
    def setUp(self):
        self.submit()
        mail.outbox.clear()
        self.response = self.submit(comments="Following up on my request.")

    def test_is_logged_as_repeat(self):
        self.assertEqual(self.outcomes(), [Outcome.NEW, Outcome.REPEAT])

    def test_does_not_create_a_second_lead(self):
        self.assertEqual(InterestedProfessional.objects.count(), 1)

    def test_sends_no_second_thankyou(self):
        self.assertEqual(_mail_to(self.EMAIL, THANKYOU_SUBJECT), [])

    def test_sends_the_already_have_details_email(self):
        self.assertEqual(len(_mail_to(self.EMAIL, ALREADY_HAVE_DETAILS_SUBJECT)), 1)

    def test_already_have_details_email_points_to_support(self):
        replies = _mail_to(self.EMAIL, ALREADY_HAVE_DETAILS_SUBJECT)
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertIn(FHI_SUPPORT_EMAIL, reply.body)

    def test_notifies_the_team_as_returning(self):
        lead = InterestedProfessional.objects.get()
        self.assertEqual(
            [m.subject for m in _team_mail()],
            [f"New pro version signup #{lead.id} (returning)"],
        )

    def test_lands_on_the_plain_thankyou_page(self):
        self.assertRedirects(self.response, reverse("pro_version_thankyou"))


class ThirdSubmissionTest(_SignupCase):
    def setUp(self):
        self.submit()
        self.submit()

    def test_after_the_cooldown_gets_the_reply_again(self):
        _expire_cooldowns()
        mail.outbox.clear()
        self.submit()
        self.assertEqual(len(_mail_to(self.EMAIL, ALREADY_HAVE_DETAILS_SUBJECT)), 1)

    def test_within_the_cooldown_gets_no_second_reply(self):
        mail.outbox.clear()
        self.submit()
        self.assertEqual(_mail_to(self.EMAIL, ALREADY_HAVE_DETAILS_SUBJECT), [])

    def test_is_logged_as_repeat(self):
        self.submit()
        self.assertEqual(self.outcomes(), [Outcome.NEW, Outcome.REPEAT, Outcome.REPEAT])


class IntroMentionTest(_SignupCase):
    def setUp(self):
        self.submit()
        mail.outbox.clear()

    def _reply_body(self):
        self.submit()
        replies = _mail_to(self.EMAIL, ALREADY_HAVE_DETAILS_SUBJECT)
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        return reply.body

    def test_mentions_the_cofactor_introduction_once_it_was_sent(self):
        InterestedProfessional.objects.update(proconnector_sent_at=timezone.now())
        self.assertIn("introducing you to Cofactor AI", self._reply_body())

    def test_says_nothing_about_an_introduction_that_was_not_sent(self):
        self.assertNotIn("Cofactor", self._reply_body())


class CappedSubmissionTest(_SignupCase):
    def setUp(self):
        for _ in range(3):
            self.submit()
            _expire_cooldowns()
        mail.outbox.clear()
        self.response = self.submit()

    def test_fourth_is_logged_as_capped(self):
        self.assertEqual(self.outcomes()[-1], Outcome.CAPPED)

    def test_fourth_sends_no_email_at_all(self):
        self.assertEqual(mail.outbox, [])

    def test_fourth_lands_on_the_plain_thankyou_page(self):
        self.assertRedirects(self.response, reverse("pro_version_thankyou"))

    def test_thankyou_page_never_says_we_already_have_details(self):
        response = self.client.get(reverse("pro_version_thankyou") + "?already=1")
        self.assertNotContains(response, "We already have your details")

    def test_fifth_is_capped_too(self):
        _expire_cooldowns()
        mail.outbox.clear()
        self.submit()
        self.assertEqual((self.outcomes()[-1], mail.outbox), (Outcome.CAPPED, []))

    def test_capped_classic_post_lands_on_the_plain_thankyou_page(self):
        response = self.client.post(
            reverse("pro_version_external_signup"), self.PAYLOAD
        )
        self.assertRedirects(
            response, reverse("pro_version_thankyou"), fetch_redirect_response=False
        )


class LegacyLeadTest(_SignupCase):
    """A lead from before the log counts as one earlier submission."""

    def test_counts_as_one_toward_the_cap(self):
        InterestedProfessional.objects.create(
            email=self.EMAIL, thankyou_email_sent=True
        )
        for _ in range(3):
            self.submit()
        self.assertEqual(
            self.outcomes(), [Outcome.REPEAT, Outcome.REPEAT, Outcome.CAPPED]
        )

    def test_unthanked_legacy_lead_gets_the_short_reply_not_the_thankyou(self):
        InterestedProfessional.objects.create(
            email=self.EMAIL, thankyou_email_sent=False
        )
        self.submit()
        self.assertEqual(
            (
                len(_mail_to(self.EMAIL, THANKYOU_SUBJECT)),
                len(_mail_to(self.EMAIL, ALREADY_HAVE_DETAILS_SUBJECT)),
            ),
            (0, 1),
        )


class PhoneMatchTest(_SignupCase):
    OTHER_EMAIL = "office@apgar.example"

    def test_same_phone_new_email_is_a_repeat_of_the_same_lead(self):
        self.submit()
        self.submit(email=self.OTHER_EMAIL, phone_number="+1 415.555.0134")
        inquiry = ProfessionalInquiry.objects.order_by("id").last()
        lead = InterestedProfessional.objects.get(email=self.EMAIL)
        self.assertEqual(
            (inquiry.outcome, inquiry.interested_professional_id),
            (Outcome.REPEAT, lead.id),
        )

    def test_phone_matches_a_lead_from_before_the_log(self):
        lead = InterestedProfessional.objects.create(
            email=self.EMAIL, phone_number="415-555-0134"
        )
        self.submit(email=self.OTHER_EMAIL, phone_number="4155550134")
        inquiry = ProfessionalInquiry.objects.get()
        self.assertEqual(inquiry.interested_professional_id, lead.id)

    def test_later_submission_from_the_matched_email_finds_the_lead(self):
        self.submit()
        self.submit(email=self.OTHER_EMAIL)
        self.submit(email=self.OTHER_EMAIL, phone_number="")
        self.assertEqual(InterestedProfessional.objects.count(), 1)

    def test_short_numbers_never_match(self):
        self.submit(phone_number="555-0100")
        self.submit(email=self.OTHER_EMAIL, phone_number="555-0100")
        self.assertEqual(InterestedProfessional.objects.count(), 2)

    def test_phone_match_sends_nothing_to_the_new_address(self):
        self.submit()
        mail.outbox.clear()
        _expire_cooldowns()
        self.submit(email=self.OTHER_EMAIL)
        self.assertEqual(
            (
                self.outcomes()[-1],
                _mail_to(self.OTHER_EMAIL, ALREADY_HAVE_DETAILS_SUBJECT),
            ),
            (Outcome.REPEAT, []),
        )

    def test_phone_match_still_tells_the_team_it_is_returning(self):
        self.submit()
        mail.outbox.clear()
        _expire_cooldowns()
        self.submit(email=self.OTHER_EMAIL)
        self.assertTrue(any("(returning)" in m.subject for m in _team_mail()))

    def test_same_address_in_other_case_still_gets_the_reply(self):
        self.submit()
        mail.outbox.clear()
        _expire_cooldowns()
        self.submit(email=self.EMAIL.upper())
        self.assertEqual(
            len(_mail_to(self.EMAIL.upper(), ALREADY_HAVE_DETAILS_SUBJECT)), 1
        )

    def test_normalize_drops_a_leading_us_one(self):
        self.assertEqual(normalize_phone("+1 (415) 555-0134"), "4155550134")


class SpamRulesTest(TestCase):
    """spam_reason on each rule kind, and what stays clean."""

    def reason(self, **fields):
        return spam_reason(
            InterestedProfessional(
                email=fields.pop("email", "a@clinic.example"), **fields
            )
        )

    def test_honeypot(self):
        self.assertIn(
            "honeypot",
            spam_reason(InterestedProfessional(email="a@b.example"), honeypot=True),
        )

    def test_seo(self):
        self.assertIn("SEO", self.reason(comments="We offer affordable SEO packages."))

    def test_backlinks(self):
        self.assertIn(
            "backlinks", self.reason(comments="High quality backlinks for you")
        )

    def test_search_ranking(self):
        self.assertIn(
            "ranking", self.reason(comments="Get to the first page of Google")
        )

    def test_crypto(self):
        self.assertIn("crypto", self.reason(comments="Invest in Bitcoin today"))

    def test_guaranteed_results(self):
        self.assertIn(
            "guaranteed", self.reason(comments="Guaranteed results in 30 days")
        )

    def test_three_links(self):
        self.assertIn(
            "links",
            self.reason(comments="http://a.example https://b.example www.c.example"),
        )

    def test_link_in_name(self):
        self.assertIn("link in name", self.reason(name="Visit https://spam.example"))

    def test_email_domain_on_spam_list(self):
        self.assertIn("domain", self.reason(email="seller@shop.ru"))

    def test_two_soft_phrases(self):
        self.assertIn(
            "web design",
            self.reason(comments="We do web design and digital marketing for clinics."),
        )

    def test_soft_phrase_with_two_links(self):
        self.assertIn(
            "virtual assistants",
            self.reason(
                comments="Hire virtual assistants: https://a.example https://b.example"
            ),
        )

    def test_one_soft_phrase_alone_is_not_spam(self):
        self.assertEqual(
            self.reason(comments="Do you offer virtual assistants for appeals?"), ""
        )

    def test_a_practice_that_outsources_billing_is_not_spam(self):
        # Two links are one soft signal; "outsource our billing" is not another.
        self.assertEqual(
            self.reason(
                comments="We outsource our billing and want help with appeals. "
                "https://apgar.example https://apgar.example/billing"
            ),
            "",
        )

    def test_a_practice_website_is_not_spam(self):
        self.assertEqual(self.reason(comments="Our site is https://apgar.example"), "")


class SpamSubmissionTest(_SignupCase):
    def setUp(self):
        self.response = self.submit(comments="Boost your clinic with our SEO services!")

    def test_is_logged_as_spam_with_its_reason(self):
        inquiry = ProfessionalInquiry.objects.get()
        self.assertEqual(
            (inquiry.outcome, inquiry.spam_reason), (Outcome.SPAM, "SEO pitch")
        )

    def test_creates_no_lead(self):
        self.assertFalse(InterestedProfessional.objects.exists())

    def test_sends_no_email(self):
        self.assertEqual(mail.outbox, [])

    def test_lands_on_the_plain_thankyou_page(self):
        self.assertRedirects(self.response, reverse("pro_version_thankyou"))

    def test_spam_does_not_count_toward_the_cap(self):
        self.submit()
        self.submit(comments="More SEO services for you")
        self.submit()
        self.submit()
        self.assertEqual(
            self.outcomes(),
            [Outcome.SPAM, Outcome.NEW, Outcome.SPAM, Outcome.REPEAT, Outcome.REPEAT],
        )


class SpamFromKnownLeadTest(_SignupCase):
    def test_sends_nothing_and_does_not_notify_the_team(self):
        self.submit()
        mail.outbox.clear()
        self.submit(comments="Guaranteed results with our crypto fund")
        self.assertEqual(mail.outbox, [])


class HoneypotTest(_SignupCase):
    def setUp(self):
        self.response = self.client.post(
            reverse("pro_version_external_signup"),
            {**self.PAYLOAD, "website": "http://bot.example"},
        )

    def test_is_logged_as_spam(self):
        inquiry = ProfessionalInquiry.objects.get()
        self.assertEqual(
            (inquiry.outcome, inquiry.spam_reason),
            (Outcome.SPAM, "honeypot field filled"),
        )

    def test_sends_nothing(self):
        self.assertEqual(mail.outbox, [])

    def test_lands_on_the_plain_thankyou_page(self):
        self.assertRedirects(
            self.response,
            reverse("pro_version_thankyou"),
            fetch_redirect_response=False,
        )


class InquiryAdminTest(_SignupCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_superuser(
            username="clara", email="clara@barton.example", password="x"
        )
        self.client.force_login(self.staff)

    def changelist(self, query=""):
        return self.client.get(
            reverse("admin:fighthealthinsurance_professionalinquiry_changelist") + query
        )

    def test_lists_each_row_with_the_persons_submission_count(self):
        self.submit()
        self.submit()
        self.submit(email="mary@walker.example", phone_number="")
        response = self.changelist()
        self.assertEqual(
            sorted(row.person_count for row in response.context["cl"].result_list),
            [1, 2, 2],
        )

    def test_spam_rows_count_per_email(self):
        self.submit(comments="Cheap SEO")
        self.submit(comments="More SEO")
        response = self.changelist()
        self.assertEqual(
            [row.person_count for row in response.context["cl"].result_list], [2, 2]
        )

    def test_rows_with_no_email_and_no_lead_each_count_alone(self):
        for _ in range(2):
            self.client.post(
                reverse("pro_version_external_signup"),
                {"website": "http://bot.example"},
            )
        response = self.changelist()
        self.assertEqual(
            [row.person_count for row in response.context["cl"].result_list], [1, 1]
        )

    def test_created_lead_is_read_only(self):
        self.submit()
        inquiry = ProfessionalInquiry.objects.get()
        response = self.client.get(
            reverse(
                "admin:fighthealthinsurance_professionalinquiry_change",
                args=[inquiry.pk],
            )
        )
        self.assertNotIn("created_lead", response.context["adminform"].form.fields)

    def test_offers_an_outcome_filter(self):
        response = self.changelist()
        self.assertIn(
            "outcome",
            [spec.field_path for spec in response.context["cl"].filter_specs],
        )

    def test_outcome_filter_lists_only_that_outcome(self):
        self.submit()
        self.submit(email="mary@walker.example", comments="Cheap SEO", phone_number="")
        response = self.changelist("?outcome__exact=spam")
        self.assertEqual(
            [row.email for row in response.context["cl"].result_list],
            ["mary@walker.example"],
        )

    def test_shows_the_spam_reason(self):
        self.submit(comments="Cheap SEO")
        self.assertContains(self.changelist(), "SEO pitch")

    def test_lead_admin_links_to_just_their_submissions(self):
        self.submit()
        self.submit()
        self.submit(email="mary@walker.example", phone_number="")
        lead = InterestedProfessional.objects.get(email=self.EMAIL)
        page = self.client.get(
            reverse(
                "admin:fighthealthinsurance_interestedprofessional_change",
                args=[lead.id],
            )
        ).content.decode()
        inquiries = reverse("admin:fighthealthinsurance_professionalinquiry_changelist")
        links = re.findall(rf'href="({re.escape(inquiries)}[^"]*)">2<', page)
        self.assertEqual(len(links), 1)
        response = self.client.get(html.unescape(links[0]))
        self.assertEqual(
            [row.email for row in response.context["cl"].result_list],
            [self.EMAIL, self.EMAIL],
        )

    def _mark_not_spam(self):
        self.submit(comments="Cheap SEO")
        inquiry = ProfessionalInquiry.objects.get()
        mail.outbox.clear()
        self.client.post(
            reverse("admin:fighthealthinsurance_professionalinquiry_changelist"),
            {"action": "mark_not_spam", "_selected_action": [inquiry.pk]},
        )
        inquiry.refresh_from_db()
        return inquiry

    def test_not_spam_action_clears_and_links_a_new_lead(self):
        inquiry = self._mark_not_spam()
        lead = InterestedProfessional.objects.get()
        self.assertEqual(
            (inquiry.outcome, inquiry.interested_professional_id, lead.email),
            (Outcome.CLEARED, lead.id, self.EMAIL),
        )

    def test_not_spam_action_sends_nothing(self):
        self._mark_not_spam()
        self.assertEqual(mail.outbox, [])

    def test_cleared_submission_counts_as_the_first(self):
        self._mark_not_spam()
        self.submit()
        self.assertEqual(self.outcomes(), [Outcome.CLEARED, Outcome.REPEAT])


class RemoveMyDataTest(_SignupCase):
    def test_deletes_the_persons_submissions_any_case(self):
        self.submit()
        self.submit(email="VIRGINIA@Apgar.example", comments="Cheap SEO")
        RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertFalse(ProfessionalInquiry.objects.exists())

    def test_keeps_other_peoples_submissions(self):
        self.submit()
        self.submit(email="mary@walker.example", phone_number="")
        RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertEqual(
            list(ProfessionalInquiry.objects.values_list("email", flat=True)),
            ["mary@walker.example"],
        )


@override_settings(FIGHT_PAPERWORK_ENABLED=True)
class RestSubmissionTest(TestCase):
    """The retired REST endpoint goes through the same helper and still
    answers 201 for every outcome. It never emails the submitter."""

    EMAIL = "susan@picotte.example"

    def post(self, **fields):
        return self.client.post(
            reverse("interested-professional-list"),
            data=json.dumps({"email": self.EMAIL, **fields}),
            content_type="application/json",
        )

    def outcomes(self):
        return list(
            ProfessionalInquiry.objects.order_by("id").values_list("outcome", flat=True)
        )

    def test_first_creates_the_lead_and_logs_it(self):
        self.assertEqual(self.post(name="Susan La Flesche Picotte").status_code, 201)
        lead = InterestedProfessional.objects.get()
        self.assertEqual(
            list(
                ProfessionalInquiry.objects.values_list(
                    "interested_professional_id", flat=True
                )
            ),
            [lead.id],
        )

    def test_repeats_and_cap_answer_201_and_never_email_the_submitter(self):
        statuses = []
        for _ in range(4):
            statuses.append(self.post().status_code)
            _expire_cooldowns()
        self.assertEqual(
            (statuses, self.outcomes(), [m for m in mail.outbox if self.EMAIL in m.to]),
            (
                [201] * 4,
                [Outcome.NEW, Outcome.REPEAT, Outcome.REPEAT, Outcome.CAPPED],
                [],
            ),
        )

    def test_spam_answers_201_and_creates_no_lead(self):
        with patch("fighthealthinsurance.utils.send_mail") as send:
            response = self.post(comments="Top SEO and backlinks")
        self.assertEqual(
            (
                response.status_code,
                InterestedProfessional.objects.exists(),
                send.called,
            ),
            (201, False, False),
        )


class SpamIsNotMatchingEvidenceTest(_SignupCase):
    OTHER_EMAIL = "ada@lovelace-clinic.example"

    def test_a_spam_row_with_someone_elses_phone_does_not_tie_them_together(self):
        self.submit()
        self.submit(email=self.OTHER_EMAIL, comments="Our SEO services can help")
        self.submit(email=self.OTHER_EMAIL, phone_number="")
        self.assertEqual(
            self.outcomes()[-1], Outcome.NEW, "a fresh address stays a new lead"
        )


class FirstSubmissionAtomicTest(_SignupCase):
    def test_a_failed_log_leaves_no_lead_behind(self):
        with patch(
            "fighthealthinsurance.pro_signups.ProfessionalInquiry.objects.create",
            side_effect=RuntimeError("log write failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertFalse(InterestedProfessional.objects.exists())


class OverlongPhoneTest(_SignupCase):
    def test_more_digits_than_any_phone_number_is_not_a_number(self):
        self.assertEqual(normalize_phone("4" * 33), "")

    def test_a_submission_with_one_is_still_logged(self):
        self.submit(phone_number="4" * 33)
        self.assertEqual(self.outcomes(), [Outcome.NEW])


class ClearedSpamCountTest(_SignupCase):
    def _spam_then_clear(self):
        self.submit(comments="Our SEO services can help")
        inquiry = ProfessionalInquiry.objects.get(outcome=Outcome.SPAM)
        self.assertTrue(mark_not_spam(inquiry))
        return InterestedProfessional.objects.get(email=self.EMAIL)

    def test_clearing_onto_a_lead_from_before_the_log_keeps_both(self):
        InterestedProfessional.objects.create(email=self.EMAIL)
        lead = self._spam_then_clear()
        self.assertEqual(prior_submissions(lead), 2)

    def test_clearing_that_makes_the_lead_counts_once(self):
        lead = self._spam_then_clear()
        self.assertEqual(prior_submissions(lead), 1)


class SendFailureLogTest(TestCase):
    ADDRESS = "rebecca@crumpler.example"

    def _logged(self, send):
        lines = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        refused = SMTPRecipientsRefused({self.ADDRESS: (550, b"no such user")})
        try:
            with patch(
                "fighthealthinsurance.pro_signups.send_fallback_email",
                side_effect=refused,
            ), patch(
                "fighthealthinsurance.followup_emails.send_fallback_email",
                side_effect=refused,
            ):
                send()
        finally:
            logger.remove(sink)
        return "".join(lines)

    def test_repeat_reply_failure_logs_no_address(self):
        logged = self._logged(
            lambda: send_already_have_details_email(
                InterestedProfessional(name="Rebecca Lee Crumpler", email=self.ADDRESS)
            )
        )
        self.assertIn("SMTPRecipientsRefused", logged)
        self.assertNotIn(self.ADDRESS, logged)

    def test_thankyou_failure_logs_no_address(self):
        pro = InterestedProfessional.objects.create(
            name="Rebecca Lee Crumpler", email=self.ADDRESS
        )
        logged = self._logged(lambda: ThankyouEmailSender().dosend(interested_pro=pro))
        self.assertIn("SMTPRecipientsRefused", logged)
        self.assertNotIn(self.ADDRESS, logged)

"""Staff actions on a new professional signup (pro_signup_actions.py).

A new signup emails only the team. The thank-you waits for a staff member to
press a button in that notification; the other buttons send the Cofactor AI
introduction or block the address. Each action replies to the notification's
thread so the other admins know it was handled.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import proconnector
from fighthealthinsurance.models import (
    InterestedProfessional,
    ProfessionalInquiry,
    ScheduledEmail,
)
from fighthealthinsurance.utils import signup_thread_message_id

Outcome = ProfessionalInquiry.Outcome

THANKYOU_SUBJECT = "Thanks for your interest in our professional version"
TEAM_INBOX = "professional@fighthealthinsurance.com"
CRUMPLER = "Rebecca Lee Crumpler <rcrumpler@fighthealthinsurance.com>"


def _mail_to(address, subject=THANKYOU_SUBJECT):
    return [m for m in mail.outbox if m.to == [address] and m.subject == subject]


def _team_mail():
    return [m for m in mail.outbox if TEAM_INBOX in m.to]


def _html(message):
    return next(c for c, mimetype in message.alternatives if mimetype == "text/html")


class _SignupCase(TestCase):
    EMAIL = "mary@walker.example"
    PAYLOAD = {
        "name": "Mary Walker",
        "email": EMAIL,
        "business_name": "Walker Surgical",
        "comments": "We fight a lot of surgery denials.",
    }

    def submit(self, **changes):
        return self.client.post(reverse("pro_version"), {**self.PAYLOAD, **changes})

    def setUp(self):
        self.submit()
        self.pro = InterestedProfessional.objects.get(email=self.EMAIL)
        self.notification = _team_mail()[0]


class NewSignupTest(_SignupCase):
    def test_sends_no_thankyou_until_staff_act(self):
        self.assertEqual(_mail_to(self.EMAIL), [])

    def test_leaves_the_lead_unthanked(self):
        self.assertFalse(self.pro.thankyou_email_sent)

    def test_stores_the_notification_subject_for_threading(self):
        self.assertEqual(
            self.pro.notification_subject, f"New pro version signup #{self.pro.id}"
        )

    def test_notification_starts_the_thread(self):
        self.assertEqual(
            self.notification.extra_headers["Message-ID"],
            signup_thread_message_id(self.pro),
        )

    def test_notification_says_nothing_has_gone_to_them(self):
        self.assertIn("Nothing has been sent to them yet.", self.notification.body)

    def test_notification_body_links_every_action(self):
        for path in (
            reverse("pro_signup_action", args=[self.pro.id, "thankyou"]),
            reverse("proconnector_quick_intro", args=[self.pro.id]),
            reverse("pro_signup_action", args=[self.pro.id, "block"]),
        ):
            self.assertIn(path, self.notification.body)

    def test_notification_html_renders_the_three_buttons(self):
        html = _html(self.notification)
        for label in (
            "Send thank-you email",
            "Send the Cofactor AI introduction",
            "Block address",
        ):
            self.assertIn(label, html)


class _StaffCase(_SignupCase):
    def setUp(self):
        super().setUp()
        get_user_model().objects.create_user(
            username="admin1",
            password="pw123",
            is_staff=True,
            first_name="Ada",
            last_name="Admin",
        )
        self.client.login(username="admin1", password="pw123")
        mail.outbox.clear()

    def url(self, action):
        return reverse("pro_signup_action", args=[self.pro.id, action])

    def thread_notes(self):
        return [m for m in _team_mail() if m.subject.startswith("Re: ")]


class AccessTest(_SignupCase):
    def test_anonymous_is_sent_to_login(self):
        url = reverse("pro_signup_action", args=[self.pro.id, "thankyou"])
        response = self.client.post(url)
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={url}",
            fetch_redirect_response=False,
        )

    def test_anonymous_post_sends_nothing(self):
        mail.outbox.clear()
        self.client.post(reverse("pro_signup_action", args=[self.pro.id, "thankyou"]))
        self.assertEqual(mail.outbox, [])


class ThankyouActionTest(_StaffCase):
    def test_get_only_previews_and_sends_nothing(self):
        response = self.client.get(self.url("thankyou"))
        self.assertEqual(
            (response.status_code, mail.outbox),
            (200, []),
        )

    def test_post_sends_the_thankyou(self):
        self.client.post(self.url("thankyou"))
        self.assertEqual(len(_mail_to(self.EMAIL)), 1)

    def test_post_marks_the_lead_thanked(self):
        self.client.post(self.url("thankyou"))
        self.pro.refresh_from_db()
        self.assertTrue(self.pro.thankyou_email_sent)

    def test_post_replies_to_the_notification_thread(self):
        self.client.post(self.url("thankyou"))
        note = self.thread_notes()[0]
        self.assertEqual(
            (
                note.subject,
                note.extra_headers["In-Reply-To"],
                note.extra_headers["References"],
            ),
            (
                f"Re: New pro version signup #{self.pro.id}",
                signup_thread_message_id(self.pro),
                signup_thread_message_id(self.pro),
            ),
        )

    def test_thread_note_names_the_admin_and_address(self):
        self.client.post(self.url("thankyou"))
        self.assertIn(
            f"Ada Admin sent the thank-you email to {self.EMAIL}",
            self.thread_notes()[0].body,
        )

    def test_second_press_sends_nothing_more(self):
        self.client.post(self.url("thankyou"))
        self.client.post(self.url("thankyou"))
        self.assertEqual(
            (len(_mail_to(self.EMAIL)), len(self.thread_notes())),
            (1, 1),
        )

    def test_failed_send_can_be_retried(self):
        with patch(
            "fighthealthinsurance.pro_signup_actions.ThankyouEmailSender.dosend",
            return_value=False,
        ):
            response = self.client.post(self.url("thankyou"))
        self.pro.refresh_from_db()
        self.assertEqual(
            (response.status_code, self.pro.thankyou_email_sent, self.thread_notes()),
            (500, False, []),
        )

    def test_refused_for_a_blocked_address(self):
        self.client.post(self.url("block"))
        self.client.post(self.url("thankyou"))
        self.assertEqual(_mail_to(self.EMAIL), [])

    def test_refused_for_an_address_we_never_email(self):
        # send_fallback_email skips such an address without raising, so the
        # send must not be recorded or announced as done.
        InterestedProfessional.objects.filter(pk=self.pro.pk).update(
            email="walker-fake@fighthealthinsurance.com"
        )
        self.client.post(self.url("thankyou"))
        self.pro.refresh_from_db()
        self.assertEqual(
            (self.pro.thankyou_email_sent, self.thread_notes()), (False, [])
        )

    def test_lead_deleted_during_the_action_goes_to_the_queue(self):
        def delete_then_note(pro, note):
            InterestedProfessional.objects.filter(pk=pro.pk).delete()

        with patch(
            "fighthealthinsurance.pro_signup_actions.post_to_signup_thread",
            side_effect=delete_then_note,
        ):
            response = self.client.post(self.url("thankyou"))
        self.assertRedirects(
            response, reverse("proconnector_process"), fetch_redirect_response=False
        )

    def test_lead_deleted_before_the_claim_goes_to_the_queue(self):
        def delete_then_fail(pro):
            InterestedProfessional.objects.filter(pk=pro.pk).delete()
            return False

        with patch(
            "fighthealthinsurance.pro_signup_actions.send_thankyou",
            side_effect=delete_then_fail,
        ):
            response = self.client.post(self.url("thankyou"))
        self.assertRedirects(
            response, reverse("proconnector_process"), fetch_redirect_response=False
        )

    def test_refused_once_the_intro_went_out(self):
        InterestedProfessional.objects.filter(pk=self.pro.pk).update(
            proconnector_attempted=True, proconnector_sent_at=timezone.now()
        )
        self.client.post(self.url("thankyou"))
        self.assertEqual(_mail_to(self.EMAIL), [])


class BlockActionTest(_StaffCase):
    def test_get_only_previews(self):
        self.client.get(self.url("block"))
        self.pro.refresh_from_db()
        self.assertFalse(self.pro.blocked)

    def test_blocks_every_lead_with_the_address(self):
        twin = InterestedProfessional.objects.create(email=self.EMAIL.upper())
        self.client.post(self.url("block"))
        self.assertEqual(
            set(
                InterestedProfessional.objects.filter(
                    pk__in=[self.pro.pk, twin.pk]
                ).values_list("blocked", flat=True)
            ),
            {True},
        )

    def test_drops_email_queued_to_the_address(self):
        ScheduledEmail.objects.create(
            to_email=self.EMAIL, template_name="proconnector_intro"
        )
        self.client.post(self.url("block"))
        self.assertFalse(ScheduledEmail.objects.filter(to_email=self.EMAIL).exists())

    def _queue_intro(self):
        InterestedProfessional.objects.filter(pk=self.pro.pk).update(
            proconnector_attempted=True, proconnector_email_body="Queued intro."
        )
        ScheduledEmail.objects.create(
            to_email=self.EMAIL,
            template_name="proconnector_intro",
            purpose="proconnector_intro",
        )

    def test_cancelled_queued_intro_can_be_queued_again_after_unblock(self):
        self._queue_intro()
        self.client.post(self.url("block"))
        InterestedProfessional.objects.filter(pk=self.pro.pk).update(blocked=False)
        self.pro.refresh_from_db()
        self.assertIsNone(proconnector.quick_intro_block_reason(self.pro))

    def test_delivered_intro_keeps_its_record(self):
        sent_at = timezone.now()
        InterestedProfessional.objects.filter(pk=self.pro.pk).update(
            proconnector_attempted=True,
            proconnector_sent_at=sent_at,
            proconnector_email_body="Sent intro.",
        )
        ScheduledEmail.objects.create(
            to_email=self.EMAIL,
            template_name="proconnector_intro",
            purpose="proconnector_intro",
        )
        self.client.post(self.url("block"))
        self.pro.refresh_from_db()
        self.assertEqual(
            (self.pro.proconnector_attempted, self.pro.proconnector_email_body),
            (True, "Sent intro."),
        )

    def test_replies_to_the_notification_thread(self):
        self.client.post(self.url("block"))
        self.assertIn(f"Ada Admin blocked {self.EMAIL}", self.thread_notes()[0].body)

    def test_second_press_posts_no_second_note(self):
        self.client.post(self.url("block"))
        self.client.post(self.url("block"))
        self.assertEqual(len(self.thread_notes()), 1)

    def test_later_submission_is_logged_as_blocked(self):
        self.client.post(self.url("block"))
        self.submit()
        self.assertEqual(
            ProfessionalInquiry.objects.order_by("-id").first().outcome,
            Outcome.BLOCKED,
        )

    def test_later_submission_emails_no_one(self):
        self.client.post(self.url("block"))
        mail.outbox.clear()
        self.submit()
        self.assertEqual(mail.outbox, [])

    def test_unknown_action_is_404(self):
        self.assertEqual(self.client.get(self.url("delete")).status_code, 404)


class BlockedLeadProConnectorTest(_StaffCase):
    def setUp(self):
        super().setUp()
        self.client.post(self.url("block"))
        self.pro.refresh_from_db()

    def test_quick_intro_reports_the_block(self):
        self.assertIn("blocked", proconnector.quick_intro_block_reason(self.pro))

    def test_cannot_be_claimed_for_an_intro(self):
        self.assertEqual(proconnector.claim_email_for_send(self.EMAIL), 0)

    def test_leaves_the_processing_queue(self):
        self.assertFalse(
            proconnector.processable_queryset().filter(pk=self.pro.pk).exists()
        )

    def test_leaves_the_bulk_mail_list(self):
        self.assertFalse(
            proconnector.mailable_interested_professionals()
            .filter(pk=self.pro.pk)
            .exists()
        )


@override_settings(COFACTOR_INTRO_CONTACT=CRUMPLER)
class QuickIntroThreadNoteTest(_StaffCase):
    @patch("fighthealthinsurance.staff_views.send_proconnector_intro_email")
    def test_sent_intro_replies_to_the_notification_thread(self, _send):
        self.client.post(
            reverse("proconnector_quick_intro", args=[self.pro.id]),
            {
                "action": "send",
                "email_body": (
                    "Introducing Rebecca, with the compensation disclosure."
                ),
            },
        )
        self.assertIn(
            f"Ada Admin sent the Cofactor AI introduction to {self.EMAIL}",
            self.thread_notes()[0].body,
        )

"""The share page's opt-in reminder: one email, 30 days on, then the address goes.

Below the request letter, a small form takes an email, and its button says
what happens. Nothing is sent before 30 days. Then the regular email pass sends one email, to the
person alone, linking back to the share page, and the address is deleted as
the email is claimed, so it is never sent twice and never kept after. Asking
again moves the date instead of adding a second email. Remove-my-data
deletes a reminder still waiting. The form has the share form's hidden field
and signed render time, and its address is never the share form's email.
"""

import datetime
import sys
import time
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core import mail
from django.db import DatabaseError
from django.db.models.deletion import Collector
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import plan_documents_reminder
from fighthealthinsurance.chat_forms import PlanDocumentsReminderForm
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import PlanDocumentsReminder, PolicyDocument
from fighthealthinsurance.shared_plan_documents import render_token

EMAIL = "rebecca.crumpler@test-fhi.com"
PAGE = "https://www.fighthealthinsurance.com/share-plan-documents"


class _ReminderTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("share_plan_documents_reminder")

    def ask(self, email=EMAIL, rendered=None, **extra):
        data = {
            "reminder-email": email,
            "rendered": (
                render_token(at=time.time() - 60) if rendered is None else rendered
            ),
        }
        data.update(extra)
        return self.client.post(self.url, data)

    def run_due(self, at):
        """One pass of the sender, as if it were ``at``."""
        sender = plan_documents_reminder.PlanDocumentsReminderSender()
        with patch.object(plan_documents_reminder, "_now", return_value=at):
            return [sender.dosend(reminder_id=pk) for pk in sender.find_candidates()]


class TheFormSitsUnderTheLetterTest(_ReminderTestCase):
    def test_the_page_offers_the_reminder_after_the_letter(self):
        soup = BeautifulSoup(
            self.client.get(reverse("share_plan_documents")).content, "html.parser"
        )
        box = soup.find(id="plan-documents-reminder")
        self.assertIn("Want a reminder?", box.get_text(" ", strip=True))
        self.assertEqual(
            box.find(id="plan-documents-reminder-intro").get_text(" ", strip=True),
            "We'll email you once, 30 days from now, so you can upload what you "
            "get. It's the only email we'll send, and we delete your address "
            "when we send it.",
        )
        # After the letter, before the other kinds of plans.
        self.assertIsNotNone(box.find_previous(id="request-letter"))
        self.assertEqual(
            box.find_next_sibling("h3").get_text(strip=True), "Other kinds of plans"
        )
        form = box.find("form")
        email = form.find("input", attrs={"name": "reminder-email"})
        self.assertEqual(email["type"], "email")
        self.assertEqual(email["autocomplete"], "email")
        self.assertTrue(email.has_attr("required"))
        self.assertIsNotNone(form.find("input", attrs={"name": "rendered"}))

    def test_the_email_and_a_button_that_says_what_happens_are_the_opt_in(self):
        soup = BeautifulSoup(
            self.client.get(reverse("share_plan_documents")).content, "html.parser"
        )
        form = soup.find(id="plan-documents-reminder-form")
        self.assertIsNone(form.find("input", attrs={"type": "checkbox"}))
        self.assertEqual(
            form.find("button", attrs={"type": "submit"}).get_text(strip=True),
            "Email me once in 30 days",
        )

    def test_a_refused_reminder_comes_back_to_its_card(self):
        soup = BeautifulSoup(
            self.client.get(reverse("share_plan_documents")).content, "html.parser"
        )
        form = soup.find(id="plan-documents-reminder-form")
        self.assertEqual(form["action"], self.url + "#plan-documents-reminder")

    def test_it_is_its_own_form_apart_from_the_share_form(self):
        soup = BeautifulSoup(
            self.client.get(reverse("share_plan_documents")).content, "html.parser"
        )
        form = soup.find(id="plan-documents-reminder").find("form")
        self.assertIsNone(form.find_parent("form"))
        share = soup.find("form", id="share-plan-documents-form")
        self.assertIsNone(share.find(attrs={"name": "reminder-email"}))
        # One id per field, though both forms have an email.
        ids = [tag["id"] for tag in soup.find_all(attrs={"id": True})]
        self.assertEqual(len(ids), len(set(ids)))

    def test_it_shows_after_sharing_too(self):
        soup = BeautifulSoup(
            self.client.get(reverse("share_plan_documents") + "?shared=1").content,
            "html.parser",
        )
        self.assertIsNotNone(soup.find(id="plan-documents-reminder-form"))

    def test_a_get_goes_back_to_the_page(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response,
            reverse("share_plan_documents") + "#plan-documents-reminder",
            fetch_redirect_response=False,
        )


class AskingForTheReminderTest(_ReminderTestCase):
    def test_asking_sets_one_reminder_30_days_on_and_says_so(self):
        before = timezone.now()
        response = self.ask(email="  Rebecca.Crumpler@Test-FHI.com ")
        self.assertRedirects(
            response,
            reverse("share_plan_documents") + "?reminder=1#plan-documents-reminder",
            fetch_redirect_response=False,
        )
        reminder = PlanDocumentsReminder.objects.get()
        self.assertEqual(reminder.email, EMAIL)
        self.assertGreaterEqual(
            reminder.send_after, before + datetime.timedelta(days=30)
        )
        self.assertLess(
            reminder.send_after, timezone.now() + datetime.timedelta(days=30, seconds=5)
        )
        page = self.client.get(response["Location"])
        self.assertContains(page, "Done. We'll email you once, 30 days from now.")
        self.assertEqual(len(mail.outbox), 0)

    def test_asking_again_moves_the_date_and_adds_no_second_email(self):
        self.ask()
        PlanDocumentsReminder.objects.update(send_after=timezone.now())
        self.ask()
        reminder = PlanDocumentsReminder.objects.get()
        self.assertGreater(
            reminder.send_after, timezone.now() + datetime.timedelta(days=29)
        )

    def test_without_an_email_nothing_is_set(self):
        response = self.ask(email="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PlanDocumentsReminder.objects.exists())
        self.assertContains(response, "This field is required.")

    def test_an_address_we_cant_send_to_is_refused(self):
        response = self.ask(email="rebecca@example.com")
        self.assertFalse(PlanDocumentsReminder.objects.exists())
        self.assertContains(response, "We can&#x27;t send email to that address.")

    def test_the_hidden_field_sets_nothing_and_says_done(self):
        response = self.ask(**{"reminder-website": "https://spam.example"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PlanDocumentsReminder.objects.exists())

    def test_a_post_that_never_drew_the_form_is_asked_again(self):
        for rendered in ("", render_token()):
            with self.subTest(rendered=rendered):
                response = self.ask(rendered=rendered)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(PlanDocumentsReminder.objects.exists())
                self.assertContains(response, "Please ask for the reminder again.")
                self.assertContains(response, f'value="{EMAIL}"')

    @override_settings(PLAN_DOCUMENTS_REMINDERS_DAILY=1)
    def test_requests_for_new_addresses_have_a_daily_limit(self):
        self.ask(email="mary.mahoney@test-fhi.com")
        response = self.ask()
        self.assertContains(response, "a lot of reminder requests today")
        self.assertEqual(PlanDocumentsReminder.objects.count(), 1)

    def test_it_never_uses_the_share_forms_email(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        shared = self.client.post(
            reverse("share_plan_documents"),
            {
                "plan_documents": [SimpleUploadedFile("sbc.txt", b"Summary")],
                "agree": "on",
                "email": EMAIL,
                "rendered": render_token(at=time.time() - 60),
            },
        )
        self.assertEqual(shared.status_code, 302)
        doc = PolicyDocument.objects.get()
        doc.document_enc.delete(save=False)
        self.assertFalse(PlanDocumentsReminder.objects.exists())
        self.assertEqual(PlanDocumentsReminderForm.PREFIX, "reminder")


class SendingTheReminderTest(_ReminderTestCase):
    def setUp(self):
        super().setUp()
        self.ask()
        self.asked_at = timezone.now()

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def test_nothing_is_sent_before_30_days(self):
        sent = self.run_due(self.asked_at + datetime.timedelta(days=29, hours=23))
        self.assertEqual(sent, [])
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(PlanDocumentsReminder.objects.exists())

    def test_at_30_days_one_email_goes_to_the_person_alone(self):
        sent = self.run_due(self.asked_at + datetime.timedelta(days=30, minutes=1))
        self.assertEqual(sent, [True])
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [EMAIL])
        self.assertFalse(message.cc)
        self.assertFalse(message.bcc)
        self.assertEqual(message.subject, plan_documents_reminder.SUBJECT)
        self.assertIn(PAGE, message.body.splitlines())
        self.assertIn("This is the only reminder we'll send", message.body)

    def test_the_address_is_gone_once_the_email_goes(self):
        self.run_due(self.asked_at + datetime.timedelta(days=30, minutes=1))
        self.assertFalse(PlanDocumentsReminder.objects.exists())
        self.assertFalse(PlanDocumentsReminder.objects.filter(email=EMAIL).exists())

    def test_it_is_sent_once(self):
        later = self.asked_at + datetime.timedelta(days=30, minutes=1)
        self.run_due(later)
        self.run_due(later + datetime.timedelta(days=1))
        self.assertEqual(len(mail.outbox), 1)

    def test_a_failed_send_still_clears_the_address_and_is_not_retried(self):
        later = self.asked_at + datetime.timedelta(days=30, minutes=1)
        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=OSError("mail server said no to rebecca.crumpler"),
        ), patch.object(plan_documents_reminder, "logger") as logged:
            sent = self.run_due(later)
        self.assertEqual(sent, [False])
        self.assertFalse(PlanDocumentsReminder.objects.exists())
        self.assertNotIn("crumpler", str(logged.mock_calls).lower())
        self.assertEqual(self.run_due(later), [])

    def test_a_failed_claim_keeps_the_address_out_of_logs_and_error_reports(self):
        later = self.asked_at + datetime.timedelta(days=30, minutes=1)
        held = []

        def fail(*args, **kwargs):
            # What an error report would show of the sender's own variables.
            frame = sys._getframe()
            while frame.f_code.co_name != "dosend":
                frame = frame.f_back
            held.extend(repr(value) for value in frame.f_locals.values())
            raise DatabaseError(f"could not delete the reminder for {EMAIL}")

        with patch.object(Collector, "delete", side_effect=fail), patch.object(
            plan_documents_reminder, "logger"
        ) as logged:
            sent = self.run_due(later)
        self.assertEqual(sent, [False])
        self.assertTrue(held)
        self.assertNotIn("crumpler", " ".join(held).lower())
        self.assertNotIn("crumpler", str(logged.mock_calls).lower())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_claimed_reminder_is_not_sent_by_a_second_sender(self):
        later = self.asked_at + datetime.timedelta(days=30, minutes=1)
        pk = PlanDocumentsReminder.objects.get().pk
        sender = plan_documents_reminder.PlanDocumentsReminderSender()
        with patch.object(plan_documents_reminder, "_now", return_value=later):
            self.assertTrue(sender.dosend(reminder_id=pk))
            self.assertFalse(sender.dosend(reminder_id=pk))
        self.assertEqual(len(mail.outbox), 1)

    def test_removing_my_data_cancels_a_waiting_reminder(self):
        RemoveDataHelper.remove_data_for_email(EMAIL.upper())
        self.assertEqual(self.run_due(self.asked_at + datetime.timedelta(days=31)), [])
        self.assertEqual(len(mail.outbox), 0)

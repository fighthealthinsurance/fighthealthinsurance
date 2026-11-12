"""The daily staff summary of shared plan documents (plan_documents_summary).

It counts, for yesterday and the 7 days up to yesterday, the files received,
read as a document, received but not readable, read but naming no company,
and the opted-in copies left out; it goes once a day to the staff list; a
quiet week sends one line on Mondays only; and it holds counts only.
"""

import datetime
import uuid
from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance import plan_documents_summary as summary
from fighthealthinsurance import shared_plan_documents
from fighthealthinsurance.models import (
    PlanSponsor,
    PolicyDocument,
    SharedPlanDocumentsDay,
)

PLAN_TEXT = "Summary Plan Description, Crumpler Cooperative Gold PPO. " * 10
STAFF = [
    "staff-one@fighthealthinsurance.example",
    "staff-two@fighthealthinsurance.example",
]
# A Wednesday, so a quiet week sends nothing unless a test says Monday.
WEDNESDAY = datetime.date(2026, 11, 11)
MONDAY = datetime.date(2026, 11, 9)


def _at(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(15), datetime.timezone.utc)


def received(
    day: datetime.date,
    status: str = PolicyDocument.PROCESSING_READY,
    text: str = PLAN_TEXT,
    company: str = "",
    **facts,
) -> PolicyDocument:
    """One shared file received on ``day``, read to ``status``."""
    if company:
        facts["plan_sponsor"] = PlanSponsor.objects.get_or_create(
            normalized_name=company.lower(), defaults={"name": company}
        )[0]
    doc = PolicyDocument.objects.create(
        source=facts.pop("source", PolicyDocument.SOURCE_SHARED),
        share_batch=uuid.uuid4(),
        fingerprint=uuid.uuid4().hex,
        processing_status=status,
        cleaned_text=text,
        **facts,
    )
    # created_at is set on save; move it to the day the test means.
    PolicyDocument.objects.filter(pk=doc.pk).update(created_at=_at(day))
    return doc


def dropped(day: datetime.date, reason: str, count: int) -> None:
    SharedPlanDocumentsDay.objects.create(day=day, name=reason, count=count)


class TheCountsTest(TestCase):
    def test_each_kind_of_file_is_counted(self):
        yesterday = WEDNESDAY - datetime.timedelta(days=1)
        received(yesterday, company="Crumpler Cooperative")
        received(yesterday)  # read, naming no company
        received(yesterday, PolicyDocument.PROCESSING_UNREADABLE, text="")
        received(yesterday, PolicyDocument.PROCESSING_RECEIVED, text="")
        dropped(yesterday, SharedPlanDocumentsDay.DROPPED_REFUSED, 2)
        counts = summary.summary_counts(WEDNESDAY)["day"]
        self.assertEqual(
            counts,
            {
                "received": 4,
                "waiting": 1,
                "read": 2,
                "unreadable": 1,
                "no_company": 1,
                "company_typed": 0,
                "dropped": 2,
            },
        )

    def test_a_company_only_the_person_typed_is_its_own_count(self):
        yesterday = WEDNESDAY - datetime.timedelta(days=1)
        doc = received(yesterday, company="Crumpler Cooperative")
        PolicyDocument.objects.filter(pk=doc.pk).update(plan_sponsor_source="person")
        counts = summary.summary_counts(WEDNESDAY)["day"]
        self.assertEqual((counts["no_company"], counts["company_typed"]), (0, 1))

    def test_yesterday_and_the_week_are_their_own_windows(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        received(WEDNESDAY - datetime.timedelta(days=5))
        received(WEDNESDAY - datetime.timedelta(days=7))
        received(WEDNESDAY - datetime.timedelta(days=8))  # before the week
        received(WEDNESDAY)  # today: tomorrow's summary
        dropped(
            WEDNESDAY - datetime.timedelta(days=3),
            SharedPlanDocumentsDay.DROPPED_OVER_BUDGET,
            4,
        )
        dropped(
            WEDNESDAY - datetime.timedelta(days=9),
            SharedPlanDocumentsDay.DROPPED_NOT_SAVED,
            9,
        )
        counts = summary.summary_counts(WEDNESDAY)
        self.assertEqual(counts["day"]["received"], 1)
        self.assertEqual(counts["week"]["received"], 3)
        self.assertEqual(counts["day"]["dropped"], 0)
        self.assertEqual(counts["week"]["dropped"], 4)

    def test_removed_files_and_spam_are_left_out(self):
        yesterday = WEDNESDAY - datetime.timedelta(days=1)
        received(yesterday, PolicyDocument.PROCESSING_REMOVED, text="")
        received(yesterday, PolicyDocument.PROCESSING_SPAM)
        self.assertEqual(summary.summary_counts(WEDNESDAY)["week"]["received"], 0)

    def test_links_and_other_uploads_are_not_files_shared(self):
        yesterday = WEDNESDAY - datetime.timedelta(days=1)
        received(yesterday, source=PolicyDocument.SOURCE_EXPLAIN_POLICY)
        self.assertEqual(summary.summary_counts(WEDNESDAY)["week"]["received"], 0)


def _request():
    request = RequestFactory().post("/")
    request.session = {}
    return request


class DroppedCopiesAreCountedTest(TestCase):
    """share_opt_in_copies leaves a copy out as before, and now counts it."""

    def setUp(self):
        self.files = [
            SimpleUploadedFile("a.pdf", b"%PDF-1.4 a", content_type="application/pdf"),
            SimpleUploadedFile("b.pdf", b"%PDF-1.4 b", content_type="application/pdf"),
        ]

    def share(self):
        return shared_plan_documents.share_opt_in_copies(
            _request(),
            self.files,
            source=PolicyDocument.SOURCE_SHARED_APPEAL,
            details={},
            hashed_email=None,
        )

    def counted(self, reason):
        return (
            SharedPlanDocumentsDay.objects.filter(
                day=timezone.now().date(), name=reason
            )
            .values_list("count", flat=True)
            .first()
            or 0
        )

    def test_refused_files(self):
        with mock.patch(
            "fighthealthinsurance.chat_forms.validate_shared_document",
            side_effect=ValidationError("not a plan document"),
        ), mock.patch.object(
            shared_plan_documents, "daily_limit_problem", return_value=None
        ):
            self.assertEqual(self.share(), [])
        self.assertEqual(self.counted(SharedPlanDocumentsDay.DROPPED_REFUSED), 2)

    def test_over_budget(self):
        with mock.patch.object(
            shared_plan_documents, "daily_limit_problem", return_value="full"
        ):
            self.assertEqual(self.share(), [])
        self.assertEqual(self.counted(SharedPlanDocumentsDay.DROPPED_OVER_BUDGET), 2)

    def test_not_saved(self):
        with mock.patch(
            "fighthealthinsurance.chat_forms.validate_shared_document"
        ), mock.patch.object(
            shared_plan_documents, "daily_limit_problem", return_value=None
        ), mock.patch.object(
            shared_plan_documents, "save_shared_documents", side_effect=RuntimeError
        ):
            self.assertEqual(self.share(), [])
        self.assertEqual(self.counted(SharedPlanDocumentsDay.DROPPED_NOT_SAVED), 2)

    def test_a_count_that_fails_never_breaks_the_page(self):
        with mock.patch.object(
            shared_plan_documents, "daily_limit_problem", return_value="full"
        ), mock.patch.object(
            SharedPlanDocumentsDay.objects, "get_or_create", side_effect=RuntimeError
        ):
            self.assertEqual(self.share(), [])

    def test_the_counts_outlive_the_daily_prune(self):
        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        names = (
            *SharedPlanDocumentsDay.DROPPED,
            SharedPlanDocumentsDay.STAFF_SUMMARY,
            SharedPlanDocumentsDay.STAFF_SUMMARY_TRIES,
        )
        for name in names:
            dropped(yesterday, name, 1)
        shared_plan_documents._drop_past_address_rows(timezone.now().date())
        self.assertEqual(
            SharedPlanDocumentsDay.objects.filter(day=yesterday).count(), len(names)
        )


@override_settings(PROFESSIONAL_SIGNUP_NOTIFICATION_EMAILS=STAFF)
class TheEmailTest(TestCase):
    def test_it_goes_to_the_staff_list_once_a_day(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        self.assertTrue(summary.send_summary_if_due(WEDNESDAY))
        self.assertFalse(summary.send_summary_if_due(WEDNESDAY))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, STAFF)
        self.assertEqual(mail.outbox[0].subject, summary.SUBJECT)

    def test_a_quiet_week_sends_nothing_midweek(self):
        self.assertFalse(summary.send_summary_if_due(WEDNESDAY))
        self.assertEqual(mail.outbox, [])

    def test_a_quiet_week_sends_one_line_on_monday(self):
        self.assertTrue(summary.send_summary_if_due(MONDAY))
        (message,) = mail.outbox
        self.assertEqual(message.subject, summary.QUIET_SUBJECT)
        self.assertEqual(len(message.body.splitlines()), 1)

    def test_the_email_holds_counts_only(self):
        yesterday = WEDNESDAY - datetime.timedelta(days=1)
        received(
            yesterday,
            company="Crumpler Cooperative",
            filename="crumpler-cooperative-sbc-2027.pdf",
            employer_name="Crumpler Cooperative",
            insurer_name="Mahoney Mutual",
            hashed_email="f" * 64,
        )
        received(
            yesterday,
            PolicyDocument.PROCESSING_UNREADABLE,
            text="",
            filename="scan-from-rebecca.pdf",
        )
        summary.send_summary_if_due(WEDNESDAY)
        (message,) = mail.outbox
        everything = message.subject + "\n" + message.body
        for secret in (
            "crumpler",
            "mahoney",
            "rebecca",
            ".pdf",
            "summary plan description",
            "f" * 64,
            "@",
        ):
            self.assertNotIn(secret, everything.lower())
        # Each line past the heading is a label and two numbers.
        rows = [line for line in message.body.splitlines() if line[:1].isalpha()]
        for _, label in summary.FIELDS:
            (row,) = [line for line in rows if line.startswith(label)]
            self.assertTrue(all(part.isdigit() for part in row[len(label) :].split()))

    @override_settings(PROFESSIONAL_SIGNUP_NOTIFICATION_EMAILS=[])
    def test_with_no_staff_list_nothing_goes_and_it_says_so(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        with mock.patch.object(summary.logger, "error") as logged:
            self.assertFalse(summary.send_summary_if_due(WEDNESDAY))
        logged.assert_called_once()
        self.assertEqual(mail.outbox, [])

    def test_a_mail_failure_is_logged_and_not_raised(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError
        ), mock.patch.object(summary.logger, "error") as logged:
            self.assertFalse(summary.send_summary_if_due(WEDNESDAY))
        logged.assert_called_once()

    def test_a_failed_send_lets_a_later_sweep_try_again(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError
        ), mock.patch.object(summary.logger, "error"):
            self.assertFalse(summary.send_summary_if_due(WEDNESDAY))
        self.assertTrue(summary.send_summary_if_due(WEDNESDAY))
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(summary.send_summary_if_due(WEDNESDAY))

    def test_a_count_that_raises_lets_a_later_sweep_try_again(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        with mock.patch.object(
            summary, "summary_counts", side_effect=RuntimeError
        ), mock.patch.object(summary.logger, "error") as logged:
            self.assertFalse(summary.send_summary_if_due(WEDNESDAY))
        logged.assert_called_once()
        self.assertTrue(summary.send_summary_if_due(WEDNESDAY))

    def test_a_broken_mail_setup_tries_a_few_times_a_day_then_stops(self):
        received(WEDNESDAY - datetime.timedelta(days=1))
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError
        ) as send, mock.patch.object(summary.logger, "error"):
            for _ in range(summary.MAX_TRIES_PER_DAY + 3):
                summary.send_summary_if_due(WEDNESDAY)
        self.assertEqual(send.call_count, summary.MAX_TRIES_PER_DAY)


class AStuckReadingShowsTest(TestCase):
    def test_files_still_waiting_are_their_own_row(self):
        received(
            WEDNESDAY - datetime.timedelta(days=2),
            PolicyDocument.PROCESSING_RECEIVED,
            text="",
        )
        counts = summary.summary_counts(WEDNESDAY)
        self.assertEqual((counts["day"]["waiting"], counts["week"]["waiting"]), (0, 1))
        body = summary.summary_text(WEDNESDAY, counts)
        self.assertIn("Still waiting to be read (should be 0: reading is stuck)", body)

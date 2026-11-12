"""A shared file's reading says why it kept no text, and a reading that
failed every try waits to be read again later, up to a cap.

The reason (PolicyDocument.unreadable_reason) is for staff: the admin row,
its filter and the cleaned text page show it, and one log line per outcome
names the row and the reason only. A reading that couldn't fetch the file
or didn't finish leaves the row waiting; the sweep reads it again
READ_RETRY_AFTER later, and after MAX_READ_ATTEMPTS it is unreadable for
good. A company lookup that fails is counted apart from "names no company".
"""

import datetime
from io import StringIO
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from loguru import logger
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.activities import shared_plan_documents as activities
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_sponsor_reader import PlanReading
from fighthealthinsurance.plan_sponsors import Sponsor
from tests.pdf_fixtures import make_pdf_bytes
from tests.plan_text_samples import PLAN_TEXT, dots_and_symbols
from tests.sync.test_shared_plan_documents_are_read_and_cleaned import (
    _ReadingTest,
    ocr_reads,
    share,
    upload,
)

User = get_user_model()

READY = PolicyDocument.PROCESSING_READY
RECEIVED = PolicyDocument.PROCESSING_RECEIVED
UNREADABLE = PolicyDocument.PROCESSING_UNREADABLE


class EachUnreadableFileSaysWhyTest(_ReadingTest):
    def reason(self, name, content):
        status, doc = self.read(name, content)
        self.assertEqual(status, UNREADABLE)
        return doc.unreadable_reason

    def test_a_locked_pdf_would_not_open(self):
        locked = make_pdf_bytes(["Member ID: W123456789"], user_password="pw")
        self.assertEqual(
            self.reason("plan.pdf", locked), PolicyDocument.UNREADABLE_CANNOT_OPEN
        )

    def test_a_broken_pdf_would_not_open(self):
        broken = b"%PDF-1.7 not a PDF after all"
        self.assertEqual(
            self.reason("plan.pdf", broken), PolicyDocument.UNREADABLE_CANNOT_OPEN
        )

    def test_a_word_file_that_will_not_open_would_not_open(self):
        self.assertEqual(
            self.reason("plan.docx", b"PK this is not a zip"),
            PolicyDocument.UNREADABLE_CANNOT_OPEN,
        )

    def test_a_text_file_of_garbage_is_garbled(self):
        self.assertEqual(
            self.reason("plan.txt", dots_and_symbols().encode()),
            PolicyDocument.UNREADABLE_GARBLED,
        )

    def test_a_scan_ocr_fails_on_every_page_waits_to_be_read_again(self):
        with ocr_reads({0: None, 1: None}):
            status, doc = self.read("scan.pdf", make_pdf_bytes(["", ""]))
        self.assertEqual(
            (status, doc.read_attempts, doc.unreadable_reason),
            (RECEIVED, 1, PolicyDocument.UNREADABLE_DID_NOT_FINISH),
        )

    def test_a_scan_ocr_finds_nothing_on(self):
        with ocr_reads({0: "", 1: ""}):
            reason = self.reason("scan.pdf", make_pdf_bytes(["", ""]))
        self.assertEqual(reason, PolicyDocument.UNREADABLE_NOTHING_AFTER_OCR)

    def no_tesseract(self):
        from fighthealthinsurance.pdf_text import TESSERACT_MISSING

        return patch(
            "fighthealthinsurance.pdf_text.aocr_pdf_pages",
            new=AsyncMock(side_effect=ValueError(TESSERACT_MISSING)),
        )

    def test_a_scan_without_tesseract_waits_to_be_read_again(self):
        with self.no_tesseract(), patch.object(processing.logger, "error"):
            status, doc = self.read("scan.pdf", make_pdf_bytes(["", ""]))
        self.assertEqual(
            (status, doc.read_attempts, doc.unreadable_reason),
            (RECEIVED, 1, PolicyDocument.UNREADABLE_OCR_UNAVAILABLE),
        )

    def test_at_the_cap_a_scan_without_tesseract_says_ocr_could_not_run(self):
        (doc,) = share(upload("scan.pdf", make_pdf_bytes(["", ""])))
        PolicyDocument.objects.filter(pk=doc.pk).update(
            read_attempts=processing.MAX_READ_ATTEMPTS - 1
        )
        with self.no_tesseract(), patch.object(processing.logger, "error"):
            self.assertEqual(self.process(doc), UNREADABLE)
        doc.refresh_from_db()
        self.assertEqual(doc.unreadable_reason, PolicyDocument.UNREADABLE_OCR_UNAVAILABLE)

    def test_the_log_line_names_the_row_and_the_reason_only(self):
        (doc,) = share(upload("plan.txt", dots_and_symbols().encode()))
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            self.process(doc)
        finally:
            logger.remove(sink)
        self.assertTrue(
            any(f"{doc.pk} is unreadable: garbled_text" in line for line in lines)
        )

    def test_a_file_read_well_has_no_reason(self):
        status, doc = self.read("plan.txt", PLAN_TEXT.encode())
        self.assertEqual((status, doc.unreadable_reason), (READY, ""))


class AReadingThatFailsEveryTryWaitsTest(_ReadingTest):
    def fail_once(self, doc, error=None):
        """One reading that fails every try, as the workflow ends it."""
        failing = (
            patch.object(processing, "_read_stored", return_value=None)
            if error is None
            else patch(
                "fighthealthinsurance.pdf_text.aocr_pdf_pages",
                new=AsyncMock(side_effect=error),
            )
        )
        with failing, self.assertRaises(ApplicationError):
            self.process(doc)
        return async_to_sync(activities.mark_shared_plan_document_unreadable)(
            str(doc.pk)
        )

    def waiting_batches(self):
        return async_to_sync(processing.awaiting_batches)(0, 10)

    def test_a_file_that_cannot_be_fetched_waits_with_its_reason(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        self.assertEqual(self.fail_once(doc), RECEIVED)
        doc.refresh_from_db()
        self.assertEqual(
            (doc.read_attempts, doc.unreadable_reason),
            (1, PolicyDocument.UNREADABLE_FILE_NOT_READ),
        )
        self.assertIsNotNone(doc.read_attempted_at)

    def test_an_ocr_that_never_finishes_waits_with_its_reason(self):
        (doc,) = share(upload("scan.pdf", make_pdf_bytes(["", ""])))
        self.fail_once(doc, TimeoutError("PDF OCR did not finish"))
        doc.refresh_from_db()
        self.assertEqual(doc.unreadable_reason, PolicyDocument.UNREADABLE_DID_NOT_FINISH)

    def test_a_share_with_nothing_due_is_not_started(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        self.fail_once(doc)
        self.assertEqual(self.waiting_batches(), [])

    def test_the_sweep_leaves_it_until_its_wait_is_over(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        self.fail_once(doc)
        self.assertEqual(self.waiting_batches(), [])
        PolicyDocument.objects.filter(pk=doc.pk).update(
            read_attempted_at=timezone.now()
            - processing.READ_RETRY_AFTER
            - datetime.timedelta(minutes=1)
        )
        self.assertEqual(self.waiting_batches(), [str(doc.share_batch)])

    def test_its_share_reads_only_rows_that_are_due(self):
        due, waiting = share(
            upload("a.txt", PLAN_TEXT.encode()), upload("b.txt", PLAN_TEXT.encode())
        )
        self.fail_once(waiting)
        ids = async_to_sync(processing.abatch_document_ids)(str(due.share_batch))
        self.assertEqual(ids, [str(due.pk)])

    def test_at_the_cap_it_is_unreadable_for_good_with_the_last_reason(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        PolicyDocument.objects.filter(pk=doc.pk).update(
            read_attempts=processing.MAX_READ_ATTEMPTS - 1
        )
        self.assertEqual(self.fail_once(doc), UNREADABLE)
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.read_attempts, doc.unreadable_reason),
            (
                UNREADABLE,
                processing.MAX_READ_ATTEMPTS,
                PolicyDocument.UNREADABLE_FILE_NOT_READ,
            ),
        )

    def test_one_below_the_cap_it_still_waits(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        PolicyDocument.objects.filter(pk=doc.pk).update(
            read_attempts=processing.MAX_READ_ATTEMPTS - 2
        )
        self.assertEqual(self.fail_once(doc), RECEIVED)

    def test_a_retried_report_of_the_same_failed_reading_counts_once(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        self.fail_once(doc)
        again = async_to_sync(activities.mark_shared_plan_document_unreadable)(
            str(doc.pk)
        )
        doc.refresh_from_db()
        self.assertEqual((again, doc.read_attempts), (RECEIVED, 1))

    def test_a_retried_report_at_the_cap_does_not_end_it_early(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        PolicyDocument.objects.filter(pk=doc.pk).update(
            read_attempts=processing.MAX_READ_ATTEMPTS - 2
        )
        self.fail_once(doc)
        again = async_to_sync(activities.mark_shared_plan_document_unreadable)(
            str(doc.pk)
        )
        doc.refresh_from_db()
        self.assertEqual(
            (again, doc.processing_status, doc.read_attempts),
            (RECEIVED, RECEIVED, processing.MAX_READ_ATTEMPTS - 1),
        )

    def test_the_cap_is_about_two_days(self):
        waits = processing.READ_RETRY_AFTER * (processing.MAX_READ_ATTEMPTS - 1)
        self.assertGreaterEqual(waits, datetime.timedelta(days=1))
        self.assertLessEqual(waits, datetime.timedelta(days=3))

    def test_a_later_reading_that_works_clears_the_reason(self):
        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        self.fail_once(doc)
        self.assertEqual(self.process(doc), READY)
        doc.refresh_from_db()
        self.assertEqual(doc.unreadable_reason, "")


class CompanyLookupsThatFailAreCountedTest(_ReadingTest):
    def test_a_lookup_that_fails_is_counted_and_the_reading_stands(self):
        with patch.object(
            processing, "afind_company", new=AsyncMock(side_effect=RuntimeError)
        ), patch.object(processing.logger, "warning"):
            status, doc = self.read("plan.txt", PLAN_TEXT.encode())
        self.assertEqual((status, doc.company_lookup_failures), (READY, 1))

    def test_a_lookup_that_finds_no_company_is_not_a_failure(self):
        with patch.object(
            processing,
            "afind_company",
            new=AsyncMock(return_value=PlanReading(None, True)),
        ):
            _, doc = self.read("plan.txt", PLAN_TEXT.encode())
        self.assertEqual(doc.company_lookup_failures, 0)

    def test_a_failure_on_a_row_removed_meanwhile_is_not_written(self):
        async def removed_then_fails(text):
            await PolicyDocument.objects.filter(pk=doc.pk).aupdate(
                processing_status=PolicyDocument.PROCESSING_REMOVED
            )
            raise RuntimeError("lookup failed")

        (doc,) = share(upload("plan.txt", PLAN_TEXT.encode()))
        with patch.object(processing, "afind_company", new=removed_then_fails):
            self.process(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.company_lookup_failures, 0)

    def test_the_failure_log_names_the_error_class_only(self):
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            with patch.object(
                processing,
                "afind_company",
                new=AsyncMock(side_effect=RuntimeError("Rebecca Lee Crumpler")),
            ):
                self.read("plan.txt", PLAN_TEXT.encode())
        finally:
            logger.remove(sink)
        self.assertFalse(any("Rebecca Lee Crumpler" in line for line in lines))
        self.assertTrue(any("to a company: RuntimeError" in line for line in lines))

    def test_link_plan_sponsors_counts_a_lookup_that_fails_now(self):
        self.read("plan.txt", PLAN_TEXT.encode())
        out, err = StringIO(), StringIO()
        with patch(
            "fighthealthinsurance.management.commands.link_plan_sponsors.afind_company",
            new=AsyncMock(side_effect=RuntimeError("Rebecca Lee Crumpler")),
        ):
            call_command("link_plan_sponsors", "--apply", stdout=out, stderr=err)
        self.assertIn("1 failed this time", out.getvalue())
        self.assertNotIn("Rebecca", err.getvalue())
        self.assertEqual(
            PolicyDocument.objects.get().company_lookup_failures, 1
        )

    def test_link_plan_sponsors_keeps_the_count_when_linking_fails(self):
        with patch.object(
            processing, "afind_company", new=AsyncMock(side_effect=RuntimeError)
        ), patch.object(processing.logger, "warning"):
            self.read("plan.txt", PLAN_TEXT.encode())
        out, err = StringIO(), StringIO()
        with patch(
            "fighthealthinsurance.management.commands.link_plan_sponsors.afind_company",
            new=AsyncMock(
                return_value=PlanReading(Sponsor("Crumpler Cooperative", ""), True)
            ),
        ), patch(
            "fighthealthinsurance.management.commands.link_plan_sponsors.link_sponsor",
            side_effect=RuntimeError("Rebecca Lee Crumpler"),
        ):
            call_command("link_plan_sponsors", "--apply", stdout=out, stderr=err)
        self.assertIn("1 failed this time", out.getvalue())
        self.assertEqual(PolicyDocument.objects.get().company_lookup_failures, 2)

    def test_link_plan_sponsors_counts_rows_whose_lookup_failed(self):
        with patch.object(
            processing, "afind_company", new=AsyncMock(side_effect=RuntimeError)
        ), patch.object(processing.logger, "warning"):
            self.read("plan.txt", PLAN_TEXT.encode())
        out = StringIO()
        with patch(
            "fighthealthinsurance.management.commands.link_plan_sponsors.afind_company",
            new=AsyncMock(return_value=PlanReading(None, True)),
        ):
            call_command("link_plan_sponsors", "--apply", stdout=out)
        self.assertIn("1 of them had a company lookup fail before", out.getvalue())
        self.assertFalse(
            PolicyDocument.objects.filter(company_lookup_failures__gt=0).exists()
        )


class StaffSeeTheReasonTest(_ReadingTest):
    def setUp(self):
        super().setUp()
        self.client.force_login(
            User.objects.create_superuser(
                username="susan_la_flesche", password="pw-for-tests", email=""
            )
        )

    def test_the_cleaned_text_page_says_why(self):
        _, doc = self.read("plan.txt", dots_and_symbols().encode())
        page = self.client.get(
            reverse("staff_policy_document_cleaned_text", kwargs={"pk": doc.pk})
        ).content.decode()
        self.assertIn("Why: Its text was garbage", page)

    def test_the_list_filters_by_reason(self):
        _, garbled = self.read("plan.txt", dots_and_symbols().encode())
        _, ready = self.read("plan2.txt", PLAN_TEXT.encode())
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"unreadable_reason__exact": PolicyDocument.UNREADABLE_GARBLED},
        ).content.decode()
        self.assertIn(str(garbled.pk), page)
        self.assertNotIn(str(ready.pk), page)

    def test_reading_again_starts_the_count_afresh(self):
        _, doc = self.read("plan.txt", dots_and_symbols().encode())
        PolicyDocument.objects.filter(pk=doc.pk).update(read_attempts=3)
        self.client.post(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"action": "read_again", "_selected_action": [str(doc.pk)]},
        )
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.read_attempts, doc.unreadable_reason),
            (RECEIVED, 0, ""),
        )

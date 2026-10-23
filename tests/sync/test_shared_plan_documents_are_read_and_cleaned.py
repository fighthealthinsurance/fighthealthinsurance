"""Shared plan documents are read and cleaned away from the request.

Each shared row is saved as received. Its reading (plan_documents_processing,
run by the SharedPlanDocumentsWorkflow activities) pulls the file's text out
with the bounded readers, takes out personal details as best it can, and
stores only the cleaned text on the row: ready for review, or unreadable
when no text could be read, as with a scanned PDF. A row removed by
remove-my-data, before or during its reading, stays removed and nothing is
written for it. The activities run for real here, against the database; the
workflow tests in tests/temporal stub them.
"""

import datetime
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.files.uploadedfile import SimpleUploadedFile
from django.forms.models import model_to_dict
from django.test import TransactionTestCase
from django.utils import timezone
from loguru import logger
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.activities import shared_plan_documents as activities
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import Denial, PolicyDocument
from fighthealthinsurance.shared_plan_documents import save_shared_documents
from tests.pdf_fixtures import make_pdf_bytes
from tests.sync.test_word_text import paragraph, word_file

EMAIL = "rebecca.crumpler@example.com"
SUMMARY = (
    "Summary of Benefits, Crumpler Mutual Health Gold PPO\n"
    "Group Number: 1234567890\n"
    "Member name: Rebecca Lee Crumpler\n"
    "Member ID: W123456789\n"
    "Questions? Call 1-800-555-0100 or write to rebecca.crumpler@example.com\n"
    "Overall deductible: $1,500\n"
)
PERSONAL = ("Rebecca Lee Crumpler", "W123456789", EMAIL)
READY = PolicyDocument.PROCESSING_READY
UNREADABLE = PolicyDocument.PROCESSING_UNREADABLE
RECEIVED = PolicyDocument.PROCESSING_RECEIVED


def upload(name, content):
    return SimpleUploadedFile(name, content)


def share(*files, email=EMAIL):
    return save_shared_documents(
        list(files),
        source=PolicyDocument.SOURCE_SHARED,
        details={"insurer_name": "Crumpler Mutual Health"},
        hashed_email=Denial.get_hashed_email(email),
    )


class _ReadingTest(TransactionTestCase):
    def tearDown(self):
        # Rows go with the test; their stored files would not.
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def process(self, doc_or_id):
        return async_to_sync(activities.process_shared_plan_document)(
            str(getattr(doc_or_id, "pk", doc_or_id))
        )

    def read(self, name, content):
        (doc,) = share(upload(name, content))
        status = self.process(doc)
        doc.refresh_from_db()
        return status, doc


class SharedRowsAreReadAndCleanedTest(_ReadingTest):
    def test_a_shared_row_starts_out_received(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, RECEIVED)
        self.assertIsNone(doc.processed_at)
        self.assertEqual(doc.cleaned_text, "")

    def test_a_text_file_is_cleaned_and_stored_ready_for_review(self):
        status, doc = self.read("plan.txt", SUMMARY.encode())
        self.assertEqual(status, READY)
        self.assertEqual(doc.processing_status, READY)
        self.assertIsNotNone(doc.processed_at)
        for kept in ("Gold PPO", "1234567890", "1-800-555-0100", "$1,500"):
            self.assertIn(kept, doc.cleaned_text)
        for gone in PERSONAL:
            self.assertNotIn(gone, doc.cleaned_text)

    def test_the_text_as_read_is_stored_nowhere(self):
        _, doc = self.read("plan.txt", SUMMARY.encode())
        for name, value in model_to_dict(doc).items():
            for gone in PERSONAL:
                with self.subTest(field=name, value=gone):
                    self.assertNotIn(gone, str(value))

    def test_a_pdf_is_read_page_by_page(self):
        content = make_pdf_bytes(
            ["Member ID: W123456789 Gold PPO", "Overall deductible: $1,500"]
        )
        status, doc = self.read("plan.pdf", content)
        self.assertEqual(status, READY)
        self.assertIn("[Page 1]", doc.cleaned_text)
        self.assertIn("[Page 2]\nOverall deductible: $1,500", doc.cleaned_text)
        self.assertNotIn("W123456789", doc.cleaned_text)

    def test_a_scanned_pdf_with_no_text_is_unreadable_not_a_failure(self):
        status, doc = self.read("scan.pdf", make_pdf_bytes(["", ""]))
        self.assertEqual(status, UNREADABLE)
        self.assertEqual(doc.processing_status, UNREADABLE)
        self.assertEqual(doc.cleaned_text, "")
        self.assertIsNotNone(doc.processed_at)

    def test_a_word_file_is_read(self):
        content = word_file(
            paragraph("Member ID: W123456789") + paragraph("Overall deductible: $1,500")
        )
        status, doc = self.read("plan.docx", content)
        self.assertEqual(status, READY)
        self.assertIn("Overall deductible: $1,500", doc.cleaned_text)
        self.assertNotIn("W123456789", doc.cleaned_text)

    def test_a_word_file_that_will_not_open_is_unreadable(self):
        status, doc = self.read("plan.docx", b"PK this is not a zip")
        self.assertEqual(status, UNREADABLE)
        self.assertEqual(doc.cleaned_text, "")

    def test_a_row_already_read_is_not_read_again(self):
        status, doc = self.read("plan.txt", SUMMARY.encode())
        first = (doc.processed_at, doc.cleaned_text)
        with patch.object(processing, "aread_text", side_effect=AssertionError):
            self.assertEqual(self.process(doc), READY)
        doc.refresh_from_db()
        self.assertEqual((doc.processed_at, doc.cleaned_text), first)

    def test_a_file_that_cannot_be_read_back_fails_then_is_marked_unreadable(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        # What read_and_decrypt_file returns when storage can't give it back.
        with patch.object(processing, "_read_stored", return_value=None):
            with self.assertRaises(ApplicationError) as raised:
                self.process(doc)
        self.assertFalse(raised.exception.non_retryable)
        self.assertEqual(
            raised.exception.message, f"FileNotRead for shared plan document {doc.pk}"
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, RECEIVED)

        status = async_to_sync(activities.mark_shared_plan_document_unreadable)(
            str(doc.pk)
        )
        self.assertEqual(status, UNREADABLE)
        doc.refresh_from_db()
        self.assertEqual((doc.processing_status, doc.cleaned_text), (UNREADABLE, ""))

    def test_an_error_names_only_its_class_and_the_row(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        secret = "Rebecca Lee Crumpler W123456789"
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            with patch.object(processing, "aread_text", side_effect=ValueError(secret)):
                with self.assertRaises(ApplicationError) as raised:
                    self.process(doc)
        finally:
            logger.remove(sink)
        self.assertEqual(
            raised.exception.message, f"ValueError for shared plan document {doc.pk}"
        )
        self.assertFalse(any(secret in line for line in lines))
        self.assertTrue(any("ValueError" in line for line in lines))


class RemovedRowsStayRemovedTest(_ReadingTest):
    def test_a_share_removed_before_its_reading_is_not_read(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        RemoveDataHelper.remove_data_for_email(EMAIL)
        listed = async_to_sync(activities.list_shared_plan_documents)(
            str(doc.share_batch)
        )
        self.assertEqual(listed, [])
        self.assertEqual(self.process(doc), processing.GONE)
        self.assertEqual(
            async_to_sync(activities.mark_shared_plan_document_unreadable)(
                str(doc.pk)
            ),
            processing.GONE,
        )
        self.assertFalse(PolicyDocument.objects.exists())

    def test_a_row_removed_during_its_reading_stays_removed(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        read = processing.aread_text

        async def removed_while_reading(ext, content):
            await PolicyDocument.objects.filter(pk=doc.pk).adelete()
            return await read(ext, content)

        with patch.object(processing, "aread_text", removed_while_reading):
            self.assertEqual(self.process(doc), processing.GONE)
        self.assertFalse(PolicyDocument.objects.exists())

    def test_rows_not_shared_for_research_are_never_read(self):
        doc = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY,
            processing_status=RECEIVED,
        )
        self.assertEqual(self.process(doc), processing.GONE)
        doc.refresh_from_db()
        self.assertIsNone(doc.processed_at)


class ShareBatchesTest(_ReadingTest):
    def share_at(self, minutes_ago, count=1):
        docs = share(*[upload(f"plan{n}.txt", SUMMARY.encode()) for n in range(count)])
        PolicyDocument.objects.filter(pk__in=[d.pk for d in docs]).update(
            shared_at=timezone.now() - datetime.timedelta(minutes=minutes_ago)
        )
        return str(docs[0].share_batch)

    def waiting(self, minutes=15, limit=50):
        return async_to_sync(processing.awaiting_batches)(minutes, limit)

    def test_a_batch_lists_only_its_rows_still_waiting(self):
        docs = share(upload("a.txt", SUMMARY.encode()), upload("b.txt", b"$1,500"))
        self.process(docs[0])
        listed = async_to_sync(activities.list_shared_plan_documents)(
            str(docs[0].share_batch)
        )
        self.assertEqual(listed, [str(docs[1].pk)])

    def test_shares_still_waiting_after_a_while_are_found(self):
        waiting = self.share_at(30)
        self.share_at(5)
        read = self.share_at(40)
        for doc in PolicyDocument.objects.filter(share_batch=read):
            self.process(doc)
        self.assertEqual(self.waiting(), [waiting])

    def test_each_share_is_found_once_oldest_first_up_to_the_limit(self):
        oldest = self.share_at(60, count=2)
        middle = self.share_at(45, count=2)
        self.share_at(30, count=2)
        self.assertEqual(self.waiting(limit=2), [oldest, middle])

    def test_rows_not_shared_for_research_are_never_found(self):
        PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY,
            processing_status=RECEIVED,
            shared_at=timezone.now() - datetime.timedelta(hours=1),
            share_batch="4f9a2b6c-2d0e-4c55-9a39-8d1b8f3d2e11",
        )
        self.assertEqual(self.waiting(), [])

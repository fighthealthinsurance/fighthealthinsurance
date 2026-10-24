"""Shared plan documents are read and cleaned away from the request.

Each shared row is saved as received. Its reading (plan_documents_processing,
run by the SharedPlanDocumentsWorkflow activities) pulls the file's text out
with the bounded readers, takes out personal details as best it can, and
stores only the cleaned text on the row: ready for review, or unreadable
when no text could be read, as with a scanned PDF. A row removed by
remove-my-data, before or during its reading, stays removed and nothing is
written for it, whether it was deleted or kept, emptied, for its plan facts. The activities run for real here, against the database; the
workflow tests in tests/temporal stub them.
"""

import datetime
import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync, sync_to_async
from django.core.files.uploadedfile import SimpleUploadedFile
from django.forms.models import model_to_dict
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from loguru import logger
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.activities import shared_plan_documents as activities
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import Denial, PolicyDocument
from fighthealthinsurance import temporal_client
from fighthealthinsurance.shared_plan_documents import (
    render_token,
    save_shared_documents,
)
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
REMOVED = PolicyDocument.PROCESSING_REMOVED
PLAN_FACTS = {"insurer_name": "Crumpler Mutual Health"}


def upload(name, content):
    return SimpleUploadedFile(name, content)


def share(*files, email=EMAIL, details=PLAN_FACTS):
    return save_shared_documents(
        list(files),
        source=PolicyDocument.SOURCE_SHARED,
        details=details,
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

    def test_a_locked_or_broken_pdf_is_unreadable_not_a_failure(self):
        for name, content in (
            ("locked", make_pdf_bytes(["Member ID: W123456789"], user_password="pw")),
            ("broken", b"%PDF-1.7 Rebecca Lee Crumpler, not a PDF after all"),
        ):
            with self.subTest(name):
                status, doc = self.read("plan.pdf", content)
                self.assertEqual(status, UNREADABLE)
                self.assertEqual(doc.cleaned_text, "")

    def test_a_pdf_read_that_times_out_or_cannot_run_is_tried_again(self):
        """Only the PDF reader's own report of a bad file ends a reading;
        anything else fails the attempt, so the activity's retries apply."""
        for error in (
            TimeoutError("PDF text extraction did not finish"),
            OSError("could not start the PDF reader"),
            RuntimeError("PDF worker exited with status -9"),
        ):
            with self.subTest(type(error).__name__):
                (doc,) = share(upload("plan.pdf", make_pdf_bytes(["Gold PPO"])))
                with patch(
                    "fighthealthinsurance.pdf_text.aextract_pdf_page_texts",
                    AsyncMock(side_effect=error),
                ):
                    with self.assertRaises(ApplicationError) as raised:
                        self.process(doc)
                self.assertFalse(raised.exception.non_retryable)
                self.assertEqual(
                    raised.exception.message,
                    f"{type(error).__name__} for shared plan document {doc.pk}",
                )
                doc.refresh_from_db()
                self.assertEqual(doc.processing_status, RECEIVED)

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
    def assertNeverRead(self, doc, expected):
        listed = async_to_sync(activities.list_shared_plan_documents)(
            str(doc.share_batch)
        )
        self.assertEqual(listed, [])
        self.assertEqual(self.process(doc), expected)
        self.assertEqual(
            async_to_sync(activities.mark_shared_plan_document_unreadable)(
                str(doc.pk)
            ),
            expected,
        )

    def test_a_share_removed_before_its_reading_is_not_read(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()), details={})
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertNeverRead(doc, processing.GONE)
        self.assertFalse(PolicyDocument.objects.exists())

    def test_a_share_kept_for_its_plan_facts_is_not_read(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertNeverRead(doc, REMOVED)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, REMOVED)
        self.assertIsNone(doc.processed_at)
        self.assertEqual(doc.cleaned_text, "")
        self.assertFalse(doc.document_enc)

    def test_a_row_emptied_during_its_reading_stays_empty(self):
        (doc,) = share(upload("plan.txt", SUMMARY.encode()))
        read = processing.aread_text

        async def removed_while_reading(ext, content):
            await sync_to_async(RemoveDataHelper.remove_data_for_email)(EMAIL)
            return await read(ext, content)

        with patch.object(processing, "aread_text", removed_while_reading):
            self.assertEqual(self.process(doc), REMOVED)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, REMOVED)
        self.assertEqual(doc.cleaned_text, "")
        self.assertIsNone(doc.processed_at)
        self.assertEqual(doc.insurer_name, "Crumpler Mutual Health")

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


class ReadingStartsOnceTheRowsCommitTest(TestCase):
    """The request that saved a share starts its reading once the rows have
    committed, from every way in: the share page and both ticked boxes all
    save through save_shared_documents. The start never holds up or breaks
    the share; the hourly sweep picks up one that was lost."""

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def test_the_share_page_starts_the_reading_of_its_batch_after_commit(self):
        seen = []

        def dispatch(share_batch):
            # Called after commit: the rows are there to read.
            seen.append((share_batch, PolicyDocument.objects.count()))
            return True

        with patch.object(temporal_client, "dispatch_shared_plan_documents", dispatch):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("share_plan_documents"),
                    {
                        "plan_documents": [upload("a.txt", b"$1,500"), upload("b.txt", b"$30")],
                        "agree": "on",
                        "rendered": render_token(at=time.time() - 60),
                    },
                )
        self.assertEqual(response.status_code, 302)
        batch = PolicyDocument.objects.values_list("share_batch", flat=True).first()
        self.assertEqual(seen, [(str(batch), 2)])

    def test_every_way_in_starts_a_reading(self):
        for source in PolicyDocument.SHARED_SOURCES:
            with self.subTest(source=source):
                with (
                    patch.object(
                        temporal_client, "dispatch_shared_plan_documents"
                    ) as dispatch,
                    self.captureOnCommitCallbacks(execute=True),
                ):
                    (doc,) = save_shared_documents(
                        [upload("plan.txt", b"$1,500")],
                        source=source,
                        details={},
                        hashed_email=None,
                    )
                dispatch.assert_called_once_with(str(doc.share_batch))

    def test_a_share_that_fails_to_save_starts_nothing(self):
        with (
            patch.object(temporal_client, "dispatch_shared_plan_documents") as dispatch,
            patch(
                "fighthealthinsurance.shared_plan_documents.fingerprint",
                side_effect=OSError,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            with self.assertRaises(OSError):
                share(upload("plan.txt", b"$1,500"))
        dispatch.assert_not_called()

    @override_settings(TEMPORAL_ENABLED=True, TEMPORAL_PLAN_DOCUMENTS_ENABLED=False)
    def test_with_the_flag_off_a_share_never_contacts_temporal(self):
        connect = AsyncMock()
        with (
            patch.object(temporal_client, "get_temporal_client", connect),
            self.captureOnCommitCallbacks(execute=True),
        ):
            share(upload("plan.txt", b"$1,500"))
        connect.assert_not_awaited()
        self.assertEqual(PolicyDocument.objects.count(), 1)

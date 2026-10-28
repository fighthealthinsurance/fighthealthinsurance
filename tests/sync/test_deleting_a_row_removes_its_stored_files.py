"""Deleting a row that owns stored files removes them from every backend.

Each file-owning model's file fields share one CombinedStorage. These tests
point it at temporary directories, store a file in every file field of a row,
delete the row through the app's own paths, and check each directory.
"""

import tempfile
from contextlib import ExitStack
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase

from loguru import logger

from fighthealthinsurance.helpers import RemoveDataHelper
from fighthealthinsurance.log_tags import email_tag, file_tag
from fighthealthinsurance.models import (
    STORED_FILE_MODELS,
    Appeal,
    AppealAttachment,
    Denial,
    ExtraLinkDocument,
    FaxesToSend,
    FollowUpDocuments,
    PlanDocuments,
    PolicyDocument,
    _file_field_names,
)

EMAIL = "rebecca.crumpler@example.com"


class _StoredFilesTestCase(TestCase):
    def setUp(self):
        super().setUp()
        dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        for d in dirs:
            self.addCleanup(d.cleanup)
        self.backends = [FileSystemStorage(location=d.name) for d in dirs]
        storages = {
            id(model._meta.get_field(f).storage): model._meta.get_field(f).storage
            for model in STORED_FILE_MODELS
            for f in _file_field_names(model)
        }
        for storage in storages.values():
            patcher = patch.object(storage, "backends", self.backends)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(EMAIL),
        )

    def with_files(self, row, upload_name="SBC.pdf"):
        """Store a file in every file field of the row and save it.

        Every upload keeps the same name, as people's own file names repeat.
        """
        for field in _file_field_names(type(row)):
            getattr(row, field).save(
                upload_name, ContentFile(b"%PDF-1.4 plan"), save=False
            )
        row.save()
        return row

    def stored_names(self, row):
        names = [getattr(row, f).name for f in _file_field_names(type(row))]
        for name in names:
            self.assertEqual([b.exists(name) for b in self.backends], [True] * 3, name)
        return names

    def copies_left(self, names):
        return [
            (n, i) for n in names for i, b in enumerate(self.backends) if b.exists(n)
        ]

    def delete_and_commit(self, row):
        names = self.stored_names(row)
        with self.captureOnCommitCallbacks(execute=True):
            row.delete()
        return self.copies_left(names)

    def appeal(self):
        return Appeal.objects.create(
            hashed_email=Denial.get_hashed_email(EMAIL),
            for_denial=self.denial,
            appeal_text="appeal",
        )

    def fax(self):
        return FaxesToSend(
            hashed_email=Denial.get_hashed_email(EMAIL),
            email=EMAIL,
            appeal_text="fax",
            paid=False,
            denial_id=self.denial,
        )


class EachModelRemovesItsFilesTest(_StoredFilesTestCase):
    def test_plan_documents_delete_removes_its_files(self):
        row = self.with_files(PlanDocuments(denial=self.denial))
        self.assertEqual(self.delete_and_commit(row), [])

    def test_follow_up_documents_delete_removes_its_files(self):
        row = self.with_files(FollowUpDocuments(denial=self.denial))
        self.assertEqual(self.delete_and_commit(row), [])

    def test_faxes_to_send_delete_removes_its_files(self):
        row = self.with_files(self.fax())
        self.assertEqual(self.delete_and_commit(row), [])

    def test_appeal_delete_removes_its_files(self):
        row = self.with_files(self.appeal())
        self.assertEqual(self.delete_and_commit(row), [])

    def test_appeal_attachment_delete_removes_its_file(self):
        row = self.with_files(
            AppealAttachment(
                appeal=self.appeal(), filename="a.pdf", mime_type="application/pdf"
            )
        )
        self.assertEqual(self.delete_and_commit(row), [])

    def test_policy_document_delete_removes_its_file(self):
        row = self.with_files(PolicyDocument(hashed_email="h"))
        self.assertEqual(self.delete_and_commit(row), [])

    def test_extra_link_document_delete_removes_its_files(self):
        row = self.with_files(
            ExtraLinkDocument(url="https://example.com/sbc.pdf", url_hash="0" * 64)
        )
        self.assertEqual(self.delete_and_commit(row), [])

    def test_the_files_stay_until_the_delete_commits(self):
        row = self.with_files(PlanDocuments(denial=self.denial))
        names = self.stored_names(row)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            row.delete()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(len(self.copies_left(names)), 3 * len(names))


class RepeatedUploadNamesTest(_StoredFilesTestCase):
    """Two uploads both named SBC.pdf, as two people's plans often are."""

    def setUp(self):
        super().setUp()
        self.first = self.with_files(PlanDocuments(denial=self.denial))
        self.second = self.with_files(PlanDocuments(denial=self.denial))
        self.first_names = self.stored_names(self.first)
        self.second_names = self.stored_names(self.second)

    def test_deleting_one_removes_all_of_its_copies(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.second.delete()
        self.assertEqual(self.copies_left(self.second_names), [])

    def test_deleting_one_keeps_every_copy_of_the_other(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.second.delete()
        self.assertEqual(
            len(self.copies_left(self.first_names)), 3 * len(self.first_names)
        )


class DenialAndRemoveDataPathsTest(_StoredFilesTestCase):
    def test_denial_delete_removes_its_plan_documents_files(self):
        plan = self.with_files(PlanDocuments(denial=self.denial))
        names = self.stored_names(plan)
        with self.captureOnCommitCallbacks(execute=True):
            Denial.objects.filter(pk=self.denial.pk).delete()
        self.assertEqual(self.copies_left(names), [])

    def test_remove_data_removes_every_stored_file_for_the_email(self):
        appeal = self.with_files(self.appeal())
        rows = [
            self.with_files(PlanDocuments(denial=self.denial)),
            self.with_files(FollowUpDocuments(denial=self.denial)),
            self.with_files(self.fax()),
            appeal,
            self.with_files(
                AppealAttachment(
                    appeal=appeal, filename="a.pdf", mime_type="application/pdf"
                )
            ),
            self.with_files(
                PolicyDocument(hashed_email=Denial.get_hashed_email(EMAIL))
            ),
        ]
        names = [n for row in rows for n in self.stored_names(row)]
        with self.captureOnCommitCallbacks(execute=True):
            RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertEqual(self.copies_left(names), [])


class SharedFilesTest(_StoredFilesTestCase):
    """A fax row stores its appeal's document under the same name."""

    def setUp(self):
        super().setUp()
        self.appeal_row = self.with_files(self.appeal())
        fax = self.fax()
        fax.combined_document_enc = self.appeal_row.document_enc
        fax.save()
        self.fax_row = fax
        self.name = self.appeal_row.document_enc.name

    def test_the_file_stays_while_another_row_uses_it(self):
        with self.captureOnCommitCallbacks(execute=True):
            FaxesToSend.objects.filter(pk=self.fax_row.pk).delete()
        self.assertEqual(len(self.copies_left([self.name])), 3)

    def test_the_file_goes_once_the_last_row_using_it_is_deleted(self):
        with self.captureOnCommitCallbacks(execute=True):
            FaxesToSend.objects.filter(pk=self.fax_row.pk).delete()
            Appeal.objects.filter(pk=self.appeal_row.pk).delete()
        self.assertEqual(self.copies_left([self.name]), [])


class StorageFailureTest(_StoredFilesTestCase):
    """The cleanup handler's log lines, captured from loguru."""

    def setUp(self):
        super().setUp()
        self.row = self.with_files(
            PolicyDocument(hashed_email="h"), upload_name="Rebecca Crumpler SBC.pdf"
        )
        self.name = self.row.document_enc.name
        self.pk = self.row.pk
        self.records: list = []
        sink_id = logger.add(lambda m: self.records.append(m.record), level="DEBUG")
        self.addCleanup(logger.remove, sink_id)

    def delete_with_failing_backends(self, *indexes):
        with ExitStack() as stack:
            for i in indexes:
                stack.enter_context(
                    patch.object(
                        self.backends[i],
                        "delete",
                        side_effect=PermissionError("read-only mount"),
                    )
                )
            stack.enter_context(self.captureOnCommitCallbacks(execute=True))
            self.row.delete()

    def handler_lines(self):
        return [
            (r["level"].name, r["message"], r["exception"])
            for r in self.records
            if r["name"] == "fighthealthinsurance.models"
        ]

    def test_a_deleted_file_is_logged_by_its_tag(self):
        self.delete_with_failing_backends()
        self.assertEqual(
            self.handler_lines(),
            [
                (
                    "INFO",
                    f"Deleted stored file {file_tag(self.name)} for"
                    f" PolicyDocument.document_enc {self.pk}",
                    None,
                )
            ],
        )

    def test_no_line_names_the_file(self):
        self.delete_with_failing_backends(0, 1)
        for record in self.records:
            self.assertNotIn("Crumpler", record["message"])

    def test_one_failing_backend_keeps_only_its_own_copy(self):
        self.delete_with_failing_backends(1)
        self.assertEqual(self.copies_left([self.name]), [(self.name, 1)])

    def test_one_failing_backend_is_logged_with_the_file_tag(self):
        self.delete_with_failing_backends(1)
        storage_errors = [
            r["message"]
            for r in self.records
            if r["name"] == "fighthealthinsurance.combined_storage"
            and r["level"].name == "ERROR"
        ]
        self.assertEqual(len(storage_errors), 1)
        self.assertIn(file_tag(self.name), storage_errors[0])
        self.assertIn("PermissionError", storage_errors[0])

    def test_one_failing_backend_raises_nothing_to_the_handler(self):
        self.delete_with_failing_backends(1)
        self.assertEqual([line[0] for line in self.handler_lines()], ["INFO"])

    def test_every_backend_failing_keeps_the_row_deleted(self):
        self.delete_with_failing_backends(0, 1, 2)
        self.assertFalse(PolicyDocument.objects.exists())

    def test_every_backend_failing_logs_one_error_tagging_the_file_without_a_traceback(
        self,
    ):
        self.delete_with_failing_backends(0, 1, 2)
        lines = self.handler_lines()
        self.assertEqual([(level, exc) for level, _m, exc in lines], [("ERROR", None)])
        for part in (
            file_tag(self.name),
            "PolicyDocument.document_enc",
            "PermissionError",
        ):
            self.assertIn(part, lines[0][1])


class RemovalLogTest(_StoredFilesTestCase):
    """A data removal logs a tag for the email, never the email or a file's name."""

    def setUp(self):
        super().setUp()
        self.with_files(
            PlanDocuments(denial=self.denial), upload_name="Rebecca Crumpler SBC.pdf"
        )
        self.messages: list = []
        sink_id = logger.add(
            lambda m: self.messages.append(m.record["message"]), level="DEBUG"
        )
        self.addCleanup(logger.remove, sink_id)
        with self.captureOnCommitCallbacks(execute=True):
            RemoveDataHelper.remove_data_for_email(EMAIL)
        self.tag = email_tag(EMAIL)

    def test_the_removal_logs_its_start_under_the_email_tag(self):
        self.assertIn(f"Data removal {self.tag} started", self.messages)

    def test_the_removal_logs_its_end_with_the_denials_removed(self):
        self.assertIn(f"Data removal {self.tag} done: 1 denials removed", self.messages)

    def test_each_deleted_file_carries_the_removal_tag(self):
        deleted = [m for m in self.messages if m.startswith("Deleted stored file")]
        self.assertEqual(len(deleted), len(_file_field_names(PlanDocuments)))
        for message in deleted:
            self.assertTrue(message.endswith(f"in data removal {self.tag}"), message)

    def test_no_line_holds_the_email_or_the_persons_name(self):
        for message in self.messages:
            self.assertNotIn("crumpler", message.lower())

"""Deleting a row that owns stored files removes them from every backend.

Each file-owning model's file fields share one CombinedStorage. These tests
point it at temporary directories, store a file in every file field of a row,
delete the row through the app's own paths, and check each directory.
"""

import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase

from fighthealthinsurance.helpers import RemoveDataHelper
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
    def test_a_failed_file_delete_is_logged_and_the_row_stays_deleted(self):
        row = self.with_files(PlanDocuments(denial=self.denial))
        with (
            patch.object(self.backends[1], "delete", side_effect=PermissionError),
            patch("fighthealthinsurance.models.logger") as log,
            self.captureOnCommitCallbacks(execute=True),
        ):
            row.delete()
        self.assertFalse(PlanDocuments.objects.exists())
        log.opt.return_value.warning.assert_called()

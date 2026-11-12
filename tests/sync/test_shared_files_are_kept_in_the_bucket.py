"""Shared plan documents are kept where they outlast the pods, and checked.

With SHARED_FILES_NEED_BUCKET_COPY a shared file counts as saved only once
the object store holds it; each keeps its size as uploaded (stored_size).
The nightly file check (shared_file_check) reads each stored copy in the
object store, with no key, and counts it whole, cut short, the wrong size,
missing or unknown, apart from files with no size recorded. It changes no
row. An encrypted copy's shape alone tells a cut copy from a whole one
(ml_document_extraction.encrypted_copy_looks_whole).
"""

import datetime
import uuid
from io import StringIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.forms.models import model_to_dict
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance import shared_file_check
from fighthealthinsurance.ml.ml_document_extraction import (
    encrypted_copy_looks_whole,
    encrypted_size,
)
from fighthealthinsurance.models import (
    Denial,
    PolicyDocument,
    SharedPlanDocumentsDay,
)
from fighthealthinsurance.shared_plan_documents import (
    NoBucketCopy,
    remove_shared_documents,
    save_shared_documents,
)
from tests.plan_text_samples import PLAN_TEXT

Day = SharedPlanDocumentsDay
EMAIL = "mary.mahoney@example.com"


def _storage():
    return PolicyDocument._meta.get_field("document_enc").storage


class _BucketTest(TransactionTestCase):
    """A bucket (an in-memory store, which is no filesystem, so an object
    store to storage_health.placement) added to the document storage."""

    def setUp(self):
        self.bucket = InMemoryStorage()
        patcher = patch.object(_storage(), "backends", [*_storage().backends, self.bucket])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def share(self, *contents):
        return save_shared_documents(
            [SimpleUploadedFile(f"plan{n}.txt", c) for n, c in enumerate(contents)],
            source=PolicyDocument.SOURCE_SHARED,
            details={},
            hashed_email=Denial.get_hashed_email(EMAIL),
        )

    def counts(self):
        return shared_file_check.check_shared_files()


class AnEncryptedCopysShapeTest(TransactionTestCase):
    def test_whole_tokens_look_whole_at_every_size(self):
        for size in (0, 1, 15, 16, 17, 47, 48, 49, 1000, 4097):
            token = Cryptographer.encrypted(b"a" * size)
            with self.subTest(size=size):
                self.assertTrue(encrypted_copy_looks_whole(token))

    def test_a_files_size_gives_its_encrypted_size(self):
        for size in (0, 1, 15, 16, 17, 47, 48, 49, 1000, 4097):
            with self.subTest(size=size):
                self.assertEqual(
                    encrypted_size(size), len(Cryptographer.encrypted(b"a" * size))
                )

    def test_a_cut_copy_rarely_looks_whole(self):
        token = Cryptographer.encrypted(PLAN_TEXT.encode() * 20)
        passing = sum(
            encrypted_copy_looks_whole(token[:cut]) for cut in range(1, len(token))
        )
        # About one cut in 64: on a 4-character boundary, fitting 57 + 16k.
        self.assertLessEqual(passing, len(token) // 50)


class ASharedFileNeedsABucketCopyTest(_BucketTest):
    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_a_file_the_bucket_holds_is_saved_with_its_size(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        doc.refresh_from_db()
        self.assertEqual(doc.stored_size, len(PLAN_TEXT.encode()))
        self.assertTrue(self.bucket.exists(doc.document_enc.name))

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_without_a_bucket_copy_nothing_is_saved(self):
        with patch.object(self.bucket, "exists", return_value=False), patch(
            "fighthealthinsurance.shared_plan_documents.logger.error"
        ):
            with self.assertRaises(NoBucketCopy):
                self.share(PLAN_TEXT.encode())
        self.assertFalse(PolicyDocument.objects.exists())

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_a_copy_written_before_the_bucket_stopped_answering_is_removed(self):
        written = []
        save = self.bucket.save

        def save_and_note(name, content, max_length=None):
            written.append(save(name, content, max_length=max_length))
            return written[-1]

        with patch.object(self.bucket, "save", side_effect=save_and_note), patch.object(
            self.bucket, "exists", return_value=False
        ), patch("fighthealthinsurance.shared_plan_documents.logger.error"):
            with self.assertRaises(NoBucketCopy):
                self.share(PLAN_TEXT.encode())
        self.assertFalse(any(self.bucket.exists(name) for name in written))

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_a_failed_save_never_removes_another_rows_copy(self):
        # The same file under the same name, shared again: a new stored name.
        (kept,) = self.share(PLAN_TEXT.encode())
        with patch.object(self.bucket, "exists", return_value=False), patch(
            "fighthealthinsurance.shared_plan_documents.logger.error"
        ):
            with self.assertRaises(NoBucketCopy):
                self.share(PLAN_TEXT.encode())
        self.assertTrue(self.bucket.exists(kept.document_enc.name))

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_a_refused_share_page_save_is_counted_for_the_day(self):
        with patch.object(self.bucket, "exists", return_value=False), patch(
            "fighthealthinsurance.shared_plan_documents.logger.error"
        ):
            with self.assertRaises(NoBucketCopy):
                self.share(PLAN_TEXT.encode(), PLAN_TEXT.encode() + b" ")
        today = timezone.now().date()
        self.assertEqual(
            Day.objects.get(day=today, name=Day.NO_BUCKET_SHARE_PAGE).count, 2
        )

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_a_refused_opted_in_copy_is_counted_apart(self):
        with patch.object(self.bucket, "exists", return_value=False), patch(
            "fighthealthinsurance.shared_plan_documents.logger.error"
        ):
            with self.assertRaises(NoBucketCopy):
                save_shared_documents(
                    [SimpleUploadedFile("plan.txt", PLAN_TEXT.encode())],
                    source=PolicyDocument.SOURCE_SHARED_APPEAL,
                    details={},
                    hashed_email=Denial.get_hashed_email(EMAIL),
                )
        today = timezone.now().date()
        self.assertEqual(Day.objects.get(day=today, name=Day.NO_BUCKET_OPT_IN).count, 1)
        self.assertFalse(
            Day.objects.filter(day=today, name=Day.NO_BUCKET_SHARE_PAGE).exists()
        )

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_a_bucket_that_cannot_answer_counts_as_no_copy(self):
        with patch.object(self.bucket, "exists", side_effect=OSError("down")), patch(
            "fighthealthinsurance.shared_plan_documents.logger.error"
        ):
            with self.assertRaises(NoBucketCopy):
                self.share(PLAN_TEXT.encode())

    @override_settings(SHARED_FILES_NEED_BUCKET_COPY=True)
    def test_with_no_object_store_configured_it_saves_and_says_so(self):
        with patch.object(_storage(), "backends", _storage().backends[:-1]), patch(
            "fighthealthinsurance.shared_plan_documents.logger.error"
        ) as error:
            (doc,) = self.share(PLAN_TEXT.encode())
        self.assertTrue(PolicyDocument.objects.filter(pk=doc.pk).exists())
        error.assert_called_once()
        self.assertIn("No object store is configured", error.call_args.args[0])

    def test_where_it_is_off_a_pod_disk_copy_is_enough(self):
        with patch.object(self.bucket, "exists", return_value=False):
            (doc,) = self.share(PLAN_TEXT.encode())
        self.assertTrue(PolicyDocument.objects.filter(pk=doc.pk).exists())


class TheNightlyFileCheckTest(_BucketTest):
    def put(self, doc, raw):
        name = doc.document_enc.name
        self.bucket.delete(name)
        self.bucket.save(name, ContentFile(raw))

    def stored(self, doc):
        with self.bucket.open(doc.document_enc.name, "rb") as f:
            return f.read()

    def test_a_whole_file_is_whole(self):
        self.share(PLAN_TEXT.encode())
        self.assertEqual(self.counts()[Day.FILE_CHECK_WHOLE], 1)

    def test_a_copy_cut_short_is_counted(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        raw = self.stored(doc)
        self.put(doc, raw[: len(raw) - 7])
        self.assertEqual(self.counts()[Day.FILE_CHECK_CUT_SHORT], 1)

    def test_a_file_with_no_size_recorded_is_counted_apart(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        PolicyDocument.objects.filter(pk=doc.pk).update(stored_size=None)
        self.assertEqual(self.counts()[Day.FILE_CHECK_NO_SIZE], 1)

    def test_a_cut_copy_with_no_size_recorded_is_still_cut_short(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        PolicyDocument.objects.filter(pk=doc.pk).update(stored_size=None)
        raw = self.stored(doc)
        self.put(doc, raw[: len(raw) - 7])
        self.assertEqual(self.counts()[Day.FILE_CHECK_CUT_SHORT], 1)

    def test_a_whole_copy_of_the_wrong_size_is_counted(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        PolicyDocument.objects.filter(pk=doc.pk).update(stored_size=10_000)
        self.assertEqual(self.counts()[Day.FILE_CHECK_WRONG_SIZE], 1)

    def test_a_file_the_bucket_lacks_is_missing(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        self.bucket.delete(doc.document_enc.name)
        self.assertEqual(self.counts()[Day.FILE_CHECK_MISSING], 1)

    def test_a_bucket_that_cannot_answer_is_unknown(self):
        self.share(PLAN_TEXT.encode())
        with patch.object(self.bucket, "exists", side_effect=OSError("down")):
            self.assertEqual(self.counts()[Day.FILE_CHECK_UNKNOWN], 1)

    def test_with_no_bucket_every_file_is_unknown(self):
        self.share(PLAN_TEXT.encode())
        with patch.object(_storage(), "backends", _storage().backends[:-1]), patch(
            "fighthealthinsurance.shared_file_check.logger.error"
        ):
            self.assertEqual(self.counts()[Day.FILE_CHECK_UNKNOWN], 1)

    def test_files_past_the_bound_are_not_checked(self):
        self.share(PLAN_TEXT.encode(), PLAN_TEXT.encode())
        with patch.object(shared_file_check, "MAX_FILES", 1):
            counts = self.counts()
        self.assertEqual(
            (counts[Day.FILE_CHECK_WHOLE], counts[Day.FILE_CHECK_NOT_CHECKED]), (1, 1)
        )

    def test_removed_files_are_not_checked(self):
        self.share(PLAN_TEXT.encode())
        remove_shared_documents(Denial.get_hashed_email(EMAIL))
        self.assertEqual(sum(self.counts().values()), 0)

    def test_it_changes_no_row(self):
        (doc,) = self.share(PLAN_TEXT.encode())
        before = model_to_dict(PolicyDocument.objects.get(pk=doc.pk))
        self.counts()
        self.assertEqual(model_to_dict(PolicyDocument.objects.get(pk=doc.pk)), before)

    def test_it_runs_once_a_day_and_keeps_its_counts(self):
        self.share(PLAN_TEXT.encode())
        today = timezone.now().date()
        self.assertTrue(shared_file_check.run_file_check_if_due(today))
        self.assertFalse(shared_file_check.run_file_check_if_due(today))
        self.assertEqual(
            Day.objects.get(day=today, name=Day.FILE_CHECK_WHOLE).count, 1
        )

    def test_a_check_that_fails_lets_a_later_sweep_try(self):
        today = timezone.now().date()
        with patch.object(
            shared_file_check, "check_shared_files", side_effect=RuntimeError
        ), patch("fighthealthinsurance.shared_file_check.logger.error"):
            self.assertFalse(shared_file_check.run_file_check_if_due(today))
        self.assertTrue(shared_file_check.run_file_check_if_due(today))

    def test_a_stalled_look_is_unknown_and_the_check_goes_on(self):
        import threading

        self.share(PLAN_TEXT.encode())
        release = threading.Event()
        self.addCleanup(release.set)
        with patch.object(shared_file_check, "SECONDS_PER_FILE", 0.2), patch(
            "fighthealthinsurance.storage_health.stored_length_and_tail",
            side_effect=lambda backend, name: release.wait(5),
        ):
            self.assertEqual(self.counts()[Day.FILE_CHECK_UNKNOWN], 1)

    def test_the_counts_outlive_the_daily_prune(self):
        from fighthealthinsurance.shared_plan_documents import _drop_past_address_rows

        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        names = (
            Day.FILE_CHECK,
            *Day.FILE_CHECK_COUNTS,
            Day.NO_BUCKET_SHARE_PAGE,
            Day.NO_BUCKET_OPT_IN,
        )
        for name in names:
            Day.objects.create(day=yesterday, name=name, count=1)
        _drop_past_address_rows(timezone.now().date())
        self.assertEqual(Day.objects.filter(day=yesterday).count(), len(names))

    def test_the_command_prints_the_counts(self):
        self.share(PLAN_TEXT.encode())
        out = StringIO()
        call_command("check_shared_files", stdout=out)
        self.assertIn("whole: 1", out.getvalue())
        self.assertIn("cut short: 0", out.getvalue())


class AnObjectStoreIsAskedForTheEndOnlyTest(TransactionTestCase):
    """storage_health.stored_length_and_tail asks an object store for the
    copy's size and a ranged read of its last bytes, never the whole file."""

    def test_it_reads_four_bytes_from_the_end(self):
        from unittest.mock import MagicMock

        from fighthealthinsurance.storage_health import stored_length_and_tail

        token = Cryptographer.encrypted(PLAN_TEXT.encode())
        client = MagicMock()
        client.stat_object.return_value.size = len(token)
        client.get_object.return_value.read.return_value = token[-4:]
        backend = MagicMock(spec=["client", "bucket_name", "open", "exists"])
        backend.client, backend.bucket_name = client, "plans"
        self.assertEqual(
            stored_length_and_tail(backend, "shared-x.txt"), (len(token), token[-4:])
        )
        client.get_object.assert_called_once_with(
            bucket_name="plans",
            object_name="shared-x.txt",
            offset=len(token) - 4,
            length=4,
        )
        backend.open.assert_not_called()

    def test_a_missing_object_is_none(self):
        from unittest.mock import MagicMock

        from fighthealthinsurance.storage_health import stored_length_and_tail

        missing = Exception("no such key")
        missing.code = "NoSuchKey"  # type: ignore[attr-defined]
        client = MagicMock()
        client.stat_object.side_effect = missing
        backend = MagicMock(spec=["client", "bucket_name"])
        backend.client, backend.bucket_name = client, "plans"
        self.assertIsNone(stored_length_and_tail(backend, "shared-x.txt"))

    def test_the_shape_from_the_end_matches_the_shape_of_the_whole(self):
        from fighthealthinsurance.ml.ml_document_extraction import (
            encrypted_shape_is_whole,
        )

        token = Cryptographer.encrypted(PLAN_TEXT.encode() * 3)
        for cut in range(1, len(token) + 1):
            copy = token[:cut]
            with self.subTest(cut=cut):
                self.assertEqual(
                    encrypted_shape_is_whole(len(copy), copy[-4:]),
                    encrypted_copy_looks_whole(copy),
                )


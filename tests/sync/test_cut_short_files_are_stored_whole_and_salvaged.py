"""Encrypted files are stored whole, and ones cut short are salvaged.

The encrypted file field hands storage its ciphertext with the size of the
file before encryption. The bucket backend sent exactly that many bytes, so
every encrypted file it kept was cut short and could not be decrypted, and
reads fell back to the ciphertext as if it were the document. Storage now
measures what it is given, and a file already cut short gives back the part
that was kept.
"""

import io

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django_encrypted_filefield.crypt import Cryptographer
from django_encrypted_filefield.fields import EncryptedFile

from fighthealthinsurance.combined_storage import CombinedStorage, correct_size
from fighthealthinsurance.ml.ml_document_extraction import (
    looks_encrypted,
    read_and_decrypt_file,
    salvage_truncated,
)
from fighthealthinsurance.models import PolicyDocument

DOCUMENT = b"%PDF-1.4 Evidence of Coverage, Crumpler Mutual Gold PPO. " * 301


class SizedBucket(Storage):
    """Stores exactly ``content.size`` bytes, as the bucket backend does."""

    def __init__(self):
        self.files = {}

    def _save(self, name, content):
        content.seek(0)
        self.files[name] = content.read(content.size)
        return name

    def exists(self, name):
        return name in self.files

    def _open(self, name, mode="rb"):
        return ContentFile(self.files[name], name=name)


class MeasuredBucket(SizedBucket):
    """A bucket that reports sizes, keeping ``drop`` bytes fewer than it is given."""

    def __init__(self, drop=0):
        super().__init__()
        self.drop = drop

    def _save(self, name, content):
        content.seek(0)
        data = content.read()
        self.files[name] = data[: len(data) - self.drop]
        return name

    def size(self, name):
        return len(self.files[name])

    def delete(self, name):
        self.files.pop(name, None)


class SizeCheckedAfterSavingTest(SimpleTestCase):
    def test_a_copy_kept_short_fails_the_save_and_leaves_nothing(self):
        disk, bucket = MeasuredBucket(), MeasuredBucket(drop=10)
        with self.assertRaises(OSError):
            CombinedStorage(disk, bucket).save("eoc.pdf", ContentFile(DOCUMENT))
        self.assertEqual(disk.files, {})
        self.assertEqual(bucket.files, {})

    def test_a_copy_saved_under_another_name_is_removed_too(self):
        class Renaming(MeasuredBucket):
            """Says every name is taken, and saves under another one."""

            def exists(self, name):
                return True

            def get_available_name(self, name, max_length=None):
                return name

            def _save(self, name, content):
                return super()._save("other-" + name, content)

        disk, bucket = Renaming(), MeasuredBucket(drop=10)
        disk.files["kept.pdf"] = b"someone else's file"
        with self.assertRaises(OSError):
            CombinedStorage(disk, bucket).save("eoc.pdf", ContentFile(DOCUMENT))
        self.assertEqual(disk.files, {"kept.pdf": b"someone else's file"})
        self.assertEqual(bucket.files, {})

    def test_content_without_a_size_still_saves(self):
        class NoSize(ContentFile):
            @property
            def size(self):
                raise io.UnsupportedOperation("no size")

            @size.setter
            def size(self, value):
                pass

        bucket = MeasuredBucket()
        name = CombinedStorage(bucket).save("eoc.pdf", NoSize(DOCUMENT))
        self.assertEqual(bucket.files[name], DOCUMENT)

    def test_whole_copies_save_as_before(self):
        disk, bucket = MeasuredBucket(), MeasuredBucket()
        name = CombinedStorage(disk, bucket).save("eoc.pdf", ContentFile(DOCUMENT))
        self.assertEqual(bucket.files[name], DOCUMENT)
        self.assertEqual(disk.files[name], DOCUMENT)

    def test_a_backend_that_cannot_report_a_size_is_trusted(self):
        bucket = SizedBucket()
        name = CombinedStorage(bucket).save("eoc.pdf", ContentFile(DOCUMENT))
        self.assertEqual(bucket.files[name], DOCUMENT)


class StoredWholeTest(SimpleTestCase):
    def test_the_bucket_keeps_all_of_an_encrypted_file(self):
        bucket = SizedBucket()
        upload = SimpleUploadedFile("eoc.pdf", DOCUMENT)
        name = CombinedStorage(bucket).save("eoc.pdf", EncryptedFile(upload))
        self.assertEqual(Cryptographer.decrypted(bucket.files[name]), DOCUMENT)

    def test_the_size_is_what_the_content_holds(self):
        content = EncryptedFile(SimpleUploadedFile("eoc.pdf", DOCUMENT))
        correct_size(content)
        self.assertEqual(content.size, len(content.getvalue()))

    def test_content_that_cannot_be_put_back_stops_the_save(self):
        class Stuck(io.BytesIO):
            def seek(self, offset, whence=0):
                if whence == 0 and offset != 0:
                    raise OSError("stuck")
                return super().seek(offset, whence)

        content = Stuck(DOCUMENT)
        content.size = 1
        content.read(5)
        with self.assertRaises(OSError):
            correct_size(content)

    def test_measuring_leaves_the_content_where_it_was(self):
        content = io.BytesIO(DOCUMENT)
        content.size = 1
        content.seek(7)
        correct_size(content)
        self.assertEqual(content.tell(), 7)


class SalvagedTest(SimpleTestCase):
    def cut(self):
        token = Cryptographer.encrypted(DOCUMENT)
        return token[: len(DOCUMENT)]

    def test_a_cut_file_gives_back_the_start_of_the_document(self):
        salvaged = salvage_truncated(self.cut())
        self.assertTrue(DOCUMENT.startswith(salvaged))

    def test_most_of_the_document_survives_the_cut(self):
        self.assertGreater(len(salvage_truncated(self.cut())), len(DOCUMENT) // 2)

    def test_a_line_of_text_is_not_mistaken_for_ciphertext(self):
        self.assertFalse(looks_encrypted(b"Summary of Benefits and Coverage"))

    def test_a_whole_file_under_another_key_gives_nothing(self):
        from cryptography.fernet import Fernet

        other = Fernet(Fernet.generate_key()).encrypt(DOCUMENT)
        self.assertEqual(salvage_truncated(other), b"")

    def test_a_whole_file_with_a_damaged_mac_gives_nothing(self):
        token = bytearray(Cryptographer.encrypted(DOCUMENT))
        token[-10] = ord("A") if token[-10] != ord("A") else ord("B")
        self.assertEqual(salvage_truncated(bytes(token)), b"")

    def test_too_little_kept_gives_nothing(self):
        self.assertEqual(salvage_truncated(b"gAAAAABq6hLE"), b"")


class ReadBackTest(TestCase):
    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def stored(self, raw):
        doc = PolicyDocument(filename="eoc.pdf")
        doc.document_enc.save("eoc.pdf", ContentFile(b"x"), save=True)
        with doc.document_enc.storage.open(doc.document_enc.name, "wb") as f:
            f.write(raw)
        return doc

    def test_a_cut_file_reads_back_as_the_start_of_the_document(self):
        token = Cryptographer.encrypted(DOCUMENT)
        doc = self.stored(token[: len(DOCUMENT)])
        got = read_and_decrypt_file(doc.document_enc)
        self.assertTrue(got and DOCUMENT.startswith(got))

    def test_a_whole_file_under_another_key_reads_back_as_nothing(self):
        from cryptography.fernet import Fernet

        doc = self.stored(Fernet(Fernet.generate_key()).encrypt(DOCUMENT))
        self.assertIsNone(read_and_decrypt_file(doc.document_enc))

    def test_a_whole_file_reads_back_whole(self):
        doc = self.stored(Cryptographer.encrypted(DOCUMENT))
        self.assertEqual(read_and_decrypt_file(doc.document_enc), DOCUMENT)


class RereadTest(TestCase):
    def row(self, **fields):
        return PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_SHARED_EXPLAIN, filename="a.pdf", **fields
        )

    def test_a_reading_of_ciphertext_goes_back_to_waiting(self):
        row = self.row(
            processing_status=PolicyDocument.PROCESSING_READY,
            cleaned_text="gAAAAABq6hLEfHGiSAtygJNwtpnmqG",
        )
        call_command("reread_shared_documents", "--apply", stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_RECEIVED)

    def test_an_unreadable_file_goes_back_to_waiting(self):
        row = self.row(processing_status=PolicyDocument.PROCESSING_UNREADABLE)
        call_command("reread_shared_documents", "--apply", stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_RECEIVED)

    def test_a_good_reading_is_left_alone(self):
        row = self.row(
            processing_status=PolicyDocument.PROCESSING_READY,
            cleaned_text="Summary of Benefits and Coverage",
        )
        call_command("reread_shared_documents", "--apply", stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_READY)

    def test_without_apply_nothing_changes(self):
        row = self.row(processing_status=PolicyDocument.PROCESSING_UNREADABLE)
        call_command("reread_shared_documents", stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.processing_status, PolicyDocument.PROCESSING_UNREADABLE)


class TooSmallToShareTest(SimpleTestCase):
    def test_a_file_too_small_to_be_a_plan_document_is_refused(self):
        from django import forms

        from fighthealthinsurance.chat_forms import validate_shared_document

        with self.assertRaises(forms.ValidationError):
            validate_shared_document(SimpleUploadedFile("test.txt", b"hello"))

    def test_a_plan_sized_text_file_is_kept(self):
        from fighthealthinsurance.chat_forms import (
            MIN_SHARED_FILE_BYTES,
            validate_shared_document,
        )

        text = b"Evidence of Coverage. " * (MIN_SHARED_FILE_BYTES // 20 + 1)
        validate_shared_document(SimpleUploadedFile("eoc.txt", text))

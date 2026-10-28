"""Each shared file carries a fingerprint, so staff can count repeats.

The fingerprint is a keyed SHA-256 of the file's contents, taken as it is
stored, on every shared row. The same file shared twice gets the same one;
the person sharing is never told whether we had it already. Keyed, so
someone holding a file can't check it against our table without our key.
The admin's list of policy documents says how many shared files there are
and how many different documents they hold.
"""

import hashlib
import time

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.shared_plan_documents import fingerprint, render_token

User = get_user_model()

SBC = b"Summary of Benefits and Coverage, Crumpler Cooperative, 2026."
SPD = b"Summary Plan Description, Crumpler Cooperative, 2026."


def txt(content, name="plan.txt"):
    return SimpleUploadedFile(name, content, content_type="text/plain")


class _ShareTestCase(TestCase):
    def setUp(self):
        self.url = reverse("share_plan_documents")

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def share(self, *contents):
        return Client().post(
            self.url,
            {
                "plan_documents": [txt(content) for content in contents],
                "agree": "on",
                "rendered": render_token(at=time.time() - 60),
            },
        )


class SharedFilesAreFingerprintedTest(_ShareTestCase):
    def test_every_shared_row_has_a_fingerprint(self):
        self.share(SBC, SPD)
        prints = list(PolicyDocument.objects.values_list("fingerprint", flat=True))
        self.assertEqual(len(prints), 2)
        for value in prints:
            with self.subTest(value=value):
                self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertNotEqual(prints[0], prints[1])

    def test_the_same_file_shared_twice_has_the_same_fingerprint(self):
        self.share(SBC)
        self.share(SBC)
        self.assertEqual(
            PolicyDocument.objects.values("fingerprint").distinct().count(), 1
        )
        self.assertEqual(PolicyDocument.objects.count(), 2)

    def test_the_fingerprint_is_keyed(self):
        self.share(SBC)
        stored = PolicyDocument.objects.get().fingerprint
        self.assertNotEqual(stored, hashlib.sha256(SBC).hexdigest())
        self.assertEqual(stored, fingerprint(txt(SBC)))

    def test_the_person_is_not_told_we_had_it_already(self):
        first = self.share(SBC)
        again = self.share(SBC)
        self.assertEqual(first.status_code, again.status_code)
        self.assertEqual(first["Location"], again["Location"])

    def test_the_stored_file_is_whole_after_fingerprinting(self):
        from fighthealthinsurance.ml.ml_document_extraction import (
            read_and_decrypt_file,
        )

        self.share(SBC)
        doc = PolicyDocument.objects.get()
        self.assertEqual(read_and_decrypt_file(doc.document_enc), SBC)

    def test_the_fingerprint_column_is_indexed(self):
        indexed = {tuple(index.fields) for index in PolicyDocument._meta.indexes}
        self.assertIn(("fingerprint",), indexed)


class StaffSeeHowManyDifferentDocumentsTest(_ShareTestCase):
    def test_the_admin_list_counts_files_and_different_documents(self):
        self.share(SBC, SPD)
        self.share(SBC)
        admin = User.objects.create_superuser(
            username="susan_la_flesche_picotte", password="pw-for-tests"
        )
        client = Client()
        client.force_login(admin)
        response = client.get(
            reverse("admin:fighthealthinsurance_policydocument_changelist")
        )
        self.assertContains(
            response, "Shared for research: 3 files, 2 different documents."
        )

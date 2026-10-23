"""Shared plan documents are stored encrypted, readable by staff, and kept.

A share needs only files and the agreement. Each file becomes one
PolicyDocument marked as shared, with the agreement's date and time. The file
is encrypted at rest but staff can read it back with the server's key, since
a file nobody can review is no use to anyone. Shared files outlive every
cleanup job, and the remove-my-data request still removes them for an email
that was given.
"""

import datetime
import io
import time
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import consent
from fighthealthinsurance.chat_forms import SharePlanDocumentsForm
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.ml.chat_policy import prune_old_chat_policies
from fighthealthinsurance.ml.ml_document_extraction import read_and_decrypt_file
from fighthealthinsurance.models import Denial, InsuranceCompany, PolicyDocument
from fighthealthinsurance.shared_plan_documents import render_token

User = get_user_model()

PDF = b"%PDF-1.4 Summary Plan Description for Crumpler Cooperative" + b" x" * 200
TXT = b"Evidence of Coverage, plan year 2026, Lee Crumpler Health Plan."
EMAIL = "Rebecca.Crumpler@example.com"


def pdf(name: str = "Rebecca Lee Crumpler SPD.pdf", content: bytes = PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def txt(name: str = "evidence of coverage.txt", content: bytes = TXT):
    return SimpleUploadedFile(name, content, content_type="text/plain")


class _ShareTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("share_plan_documents")

    def tearDown(self):
        # Rows roll back with the test; their stored files would not.
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def post(self, data):
        # A form drawn a minute ago, as a person would send it.
        data = {"rendered": render_token(at=time.time() - 60), **data}
        return self.client.post(self.url, data)

    def share(self, files, **fields):
        data = {"plan_documents": files, "agree": "on"}
        data.update(fields)
        return self.post(data)


class SharingStoresTheFilesTest(_ShareTestCase):
    def test_files_and_the_agreement_alone_store_a_shared_row(self):
        response = self.share([pdf()])
        self.assertRedirects(response, self.url + "?shared=1")
        doc = PolicyDocument.objects.get()
        self.assertEqual(doc.source, PolicyDocument.SOURCE_SHARED)
        self.assertEqual(doc.shared_consent_version, consent.SHARING_VERSION)
        self.assertIsNotNone(doc.shared_at)
        self.assertEqual(doc.plan_category, "unknown")
        self.assertIsNone(doc.hashed_email)
        self.assertFalse(doc.session_key)

    def test_the_file_at_rest_is_not_the_plaintext(self):
        self.share([pdf()])
        doc = PolicyDocument.objects.get()
        with doc.document_enc.open("rb") as stored:
            raw = stored.read()
        self.assertNotEqual(raw, PDF)
        self.assertNotIn(b"Crumpler", raw)

    def test_the_server_key_reads_the_file_back(self):
        self.share([pdf()])
        doc = PolicyDocument.objects.get()
        self.assertEqual(read_and_decrypt_file(doc.document_enc), PDF)

    def test_the_stored_name_does_not_carry_the_persons_file_name(self):
        self.share([pdf()])
        doc = PolicyDocument.objects.get()
        self.assertNotIn("Crumpler", doc.filename)
        self.assertNotIn("Crumpler", doc.document_enc.name)
        self.assertTrue(doc.filename.endswith(".pdf"))

    def test_files_shared_together_share_a_batch(self):
        self.share([pdf(), txt()])
        docs = list(PolicyDocument.objects.all())
        self.assertEqual(len(docs), 2)
        self.assertEqual(len({doc.share_batch for doc in docs}), 1)
        self.assertIsNotNone(docs[0].share_batch)

    def test_sharing_does_not_open_the_chat_for_this_visitor(self):
        self.share([pdf()])
        self.assertNotIn("consent_completed", self.client.session)
        self.assertNotIn("policy_document_id", self.client.session)

    def test_the_optional_details_are_kept_as_given(self):
        self.share(
            [pdf()],
            plan_category="employer_erisa",
            insurer_name="  Crumpler Mutual  ",
            employer_name="Lee Crumpler Clinic",
            plan_year="2026",
            state="MA",
        )
        doc = PolicyDocument.objects.get()
        self.assertEqual(doc.plan_category, "employer_erisa")
        self.assertEqual(doc.insurer_name, "Crumpler Mutual")
        self.assertEqual(doc.employer_name, "Lee Crumpler Clinic")
        self.assertEqual(doc.plan_year, 2026)
        self.assertEqual(doc.state, "MA")

    def test_a_known_insurer_name_links_to_the_catalog(self):
        company = InsuranceCompany.objects.create(
            name="Crumpler Health Mutual", alt_names="CHM\nCrumpler Mutual"
        )
        self.share([pdf()], insurer_name="crumpler mutual")
        self.assertEqual(PolicyDocument.objects.get().insurance_company_obj, company)

    def test_an_unknown_insurer_name_is_kept_without_a_link(self):
        self.share([pdf()], insurer_name="Nobody Has Heard Of Us Health")
        doc = PolicyDocument.objects.get()
        self.assertIsNone(doc.insurance_company_obj)
        self.assertEqual(doc.insurer_name, "Nobody Has Heard Of Us Health")


class SharingRejectsTest(_ShareTestCase):
    def assertNothingStored(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 0)

    def test_without_the_agreement_nothing_is_stored(self):
        response = self.post({"plan_documents": [pdf()]})
        self.assertNothingStored(response)
        self.assertContains(response, 'id="share-errors"')

    def test_each_error_is_read_out_with_its_field(self):
        response = self.post({"plan_year": "1999"})
        soup = BeautifulSoup(response.content, "html.parser")
        for name in ("plan_documents", "plan_year", "agree"):
            with self.subTest(field=name):
                field = soup.find(attrs={"name": name})
                described_by = field["aria-describedby"].split()
                self.assertIn(f"{field['id']}_error", described_by)
                note = soup.find(id=f"{field['id']}_error")
                self.assertTrue(note.get_text(strip=True))
        self.assertContains(response, "Tick the box to agree before you share.")

    def test_without_a_file_nothing_is_stored(self):
        response = self.post({"agree": "on"})
        self.assertNothingStored(response)
        self.assertContains(response, "Choose at least one file to share.")

    def test_an_unsupported_file_type_is_rejected(self):
        bad = SimpleUploadedFile("plan.exe", b"MZ\x90\x00", content_type="text/plain")
        response = self.share([bad])
        self.assertNothingStored(response)
        self.assertContains(response, "Unsupported file type")

    def test_a_pdf_that_is_not_a_pdf_is_rejected(self):
        response = self.share([pdf(content=b"<html>not a pdf</html>")])
        self.assertNothingStored(response)
        self.assertContains(response, "does not appear to be a valid PDF")

    def test_one_bad_file_stops_the_whole_share(self):
        bad = SimpleUploadedFile("plan.zip", b"PK\x03\x04", content_type="text/plain")
        response = self.share([pdf(), bad])
        self.assertNothingStored(response)

    def test_more_than_the_limit_of_files_is_rejected(self):
        files = [
            txt(name=f"page {n}.txt")
            for n in range(SharePlanDocumentsForm.MAX_FILES + 1)
        ]
        response = self.share(files)
        self.assertNothingStored(response)

    def test_a_plan_year_out_of_range_is_rejected(self):
        response = self.share([pdf()], plan_year="1999")
        self.assertNothingStored(response)
        about = BeautifulSoup(response.content, "html.parser").find(
            "details", id="about-the-plan"
        )
        self.assertTrue(about.has_attr("open"))

    def test_a_filled_honeypot_stores_nothing_and_says_thanks(self):
        response = self.share([pdf()], website="https://spam.example")
        self.assertRedirects(response, self.url + "?shared=1")
        self.assertEqual(PolicyDocument.objects.count(), 0)

    def test_a_failed_save_keeps_no_rows_and_deletes_the_files_it_wrote(self):
        stored_names = []
        real_save = PolicyDocument.save

        def save_then_fail(doc, *args, **kwargs):
            if stored_names:
                raise RuntimeError("storage went away")
            real_save(doc, *args, **kwargs)
            stored_names.append(doc.document_enc.name)

        storage = PolicyDocument._meta.get_field("document_enc").storage
        with (
            patch.object(PolicyDocument, "save", save_then_fail),
            patch.object(storage, "delete", wraps=storage.delete) as deleted,
        ):
            response = self.share([pdf(), txt()])
        self.assertNothingStored(response)
        self.assertContains(response, "We couldn&#x27;t save your files.")
        deleted.assert_any_call(stored_names[0])


class SharedEmailIsOnlyHashedTest(_ShareTestCase):
    def test_an_email_is_stored_only_as_its_hash(self):
        self.share([pdf()], email=EMAIL)
        doc = PolicyDocument.objects.get()
        self.assertEqual(doc.hashed_email, Denial.get_hashed_email(EMAIL.lower()))
        for field in PolicyDocument._meta.concrete_fields:
            value = str(field.value_from_object(doc) or "")
            with self.subTest(field=field.name):
                self.assertNotIn("crumpler@example.com", value.lower())


class StaffReadSharedFilesTest(_ShareTestCase):
    def setUp(self):
        super().setUp()
        self.share([pdf()])
        self.doc = PolicyDocument.objects.get()
        self.download = reverse(
            "staff_policy_document_download", kwargs={"pk": self.doc.pk}
        )

    def test_staff_download_the_decrypted_file(self):
        staff = User.objects.create_user(
            username="sarah_loguen_fraser", password="pw-for-tests", is_staff=True
        )
        self.client.force_login(staff)
        response = self.client.get(self.download)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PDF)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_people_who_are_not_staff_cannot_download(self):
        member = User.objects.create_user(
            username="mary_eliza_mahoney", password="pw-for-tests", is_staff=False
        )
        self.client.force_login(member)
        response = self.client.get(self.download)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"Crumpler", response.content)

    def test_visitors_cannot_download(self):
        response = Client().get(self.download)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"Crumpler", response.content)

    def test_the_admin_links_staff_to_the_download(self):
        admin = User.objects.create_superuser(
            username="susan_la_flesche_picotte", password="pw-for-tests"
        )
        self.client.force_login(admin)
        page = self.client.get(
            reverse(
                "admin:fighthealthinsurance_policydocument_change",
                args=[self.doc.pk],
            )
        )
        self.assertContains(page, self.download)


class SharedFilesAreKeptTest(_ShareTestCase):
    def test_shared_files_survive_every_cleanup_job(self):
        self.share([pdf()])
        long_ago = timezone.now() - datetime.timedelta(days=3 * 365)
        PolicyDocument.objects.update(created_at=long_ago, shared_at=long_ago)
        for command in (
            "clearsessions",
            "clear_expired_emails",
            "sweep_assistant_drafts",
            "sweep_assistant_handoffs",
        ):
            call_command(command, stdout=io.StringIO())
        prune_old_chat_policies()
        doc = PolicyDocument.objects.get()
        self.assertEqual(read_and_decrypt_file(doc.document_enc), PDF)

    def test_removing_my_data_removes_files_shared_with_that_email(self):
        self.share([pdf()], email=EMAIL)
        self.share([txt()])
        RemoveDataHelper.remove_data_for_email(EMAIL)
        remaining = PolicyDocument.objects.get()
        self.assertIsNone(remaining.hashed_email)


class ExplainPolicyUploadsAreNotSharedTest(TestCase):
    def test_an_explain_policy_upload_is_not_marked_shared(self):
        Client().post(
            reverse("understand_policy"),
            {
                "first_name": "Rebecca",
                "last_name": "Crumpler",
                "email": EMAIL,
                "document_type": "summary_of_benefits",
                "plan_category": "unknown",
                "tos_agreement": "on",
                "privacy_policy": "on",
                "policy_document": pdf(),
            },
        )
        doc = PolicyDocument.objects.get()
        self.assertEqual(doc.source, PolicyDocument.SOURCE_EXPLAIN_POLICY)
        self.assertIsNone(doc.shared_consent_version)
        doc.document_enc.delete(save=False)

"""Large files are bounded on the way in and on the way back out.

Every page that keeps an uploaded file (the appeal flow's plan documents,
follow-up feedback, server-side OCR) checks its size, count and -- where a
reader later parses it -- its format before a single file is stored, so an
oversized or unreadable file is never written to the shared volume and then
counted on the page as added. The readers of stored policy documents all use
one ceiling, sized so an encrypted upload at the 20MB limit (about 26.7MB
once encrypted) still reads, and CombinedStorage.save does not keep a file a
timed-out write left half-written.
"""

from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import upload_limits, views
from fighthealthinsurance.combined_storage import CombinedStorage, save_timeout_seconds
from fighthealthinsurance.forms import FollowUpForm, PlanDocumentsForm
from fighthealthinsurance.models import (
    Denial,
    FollowUpDocuments,
    PlanDocuments,
    PolicyDocument,
)
from tests.back_links import back_link

EMAIL = "largefile.tester@test-fhi.com"
SEMI_SEKRET = "sekret-for-large-files"


def _upload(name, size, content=None):
    body = content if content is not None else b"a" * size
    return SimpleUploadedFile(name, body, content_type="application/octet-stream")


def _pdf(name="plan.pdf", size=1024):
    body = b"%PDF-1.4 " + b"x" * max(0, size - 9)
    return SimpleUploadedFile(name, body, content_type="application/pdf")


class StoredSizeLimitMatchesTheEncryptedUploadTest(TestCase):
    """The ceiling every stored-document reader uses must cover an upload at
    the limit once it is encrypted, or files are silently dropped."""

    def test_the_read_ceiling_covers_an_encrypted_max_upload(self):
        from cryptography.fernet import Fernet

        token = len(Fernet(Fernet.generate_key()).encrypt(
            b"a" * upload_limits.MAX_POLICY_DOCUMENT_BYTES
        ))
        self.assertLessEqual(
            token, upload_limits.MAX_STORED_POLICY_DOCUMENT_BYTES
        )

    def test_every_reader_shares_the_one_ceiling(self):
        from fighthealthinsurance import plan_documents_processing, staff_views
        from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper

        expected = upload_limits.MAX_STORED_POLICY_DOCUMENT_BYTES
        self.assertEqual(MLPlanDocHelper.MAX_DOCUMENT_BYTES, expected)
        self.assertEqual(plan_documents_processing.MAX_STORED_BYTES, expected)
        self.assertEqual(staff_views.PolicyDocumentTextView.MAX_STORED_BYTES, expected)


class PlanDocumentsFormChecksFilesTest(TestCase):
    def test_a_file_over_the_limit_is_refused(self):
        form = PlanDocumentsForm(
            data={"denial_id": 1, "email": EMAIL, "semi_sekret": SEMI_SEKRET},
            files={
                "plan_documents": _pdf(
                    size=upload_limits.MAX_UPLOAD_FILE_BYTES + 1
                )
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("plan_documents", form.errors)
        self.assertIn("20MB", " ".join(form.errors["plan_documents"]))

    def test_an_unreadable_format_is_refused(self):
        form = PlanDocumentsForm(
            data={"denial_id": 1, "email": EMAIL, "semi_sekret": SEMI_SEKRET},
            files={"plan_documents": _upload("plan.exe", 50)},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("plan_documents", form.errors)

    def test_too_many_files_are_refused(self):
        form = PlanDocumentsForm(
            data={"denial_id": 1, "email": EMAIL, "semi_sekret": SEMI_SEKRET},
            files={
                "plan_documents": [
                    _pdf(name=f"p{i}.pdf")
                    for i in range(upload_limits.MAX_UPLOAD_FILES + 1)
                ]
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("plan_documents", form.errors)

    def test_files_within_the_limits_pass(self):
        form = PlanDocumentsForm(
            data={"denial_id": 1, "email": EMAIL, "semi_sekret": SEMI_SEKRET},
            files={"plan_documents": [_pdf(), _upload("notes.txt", 20)]},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_no_files_is_fine(self):
        form = PlanDocumentsForm(
            data={"denial_id": 1, "email": EMAIL, "semi_sekret": SEMI_SEKRET},
        )
        self.assertTrue(form.is_valid(), form.errors)


class FollowUpFormChecksFilesTest(TestCase):
    def _base(self):
        import uuid

        return {
            "uuid": str(uuid.uuid4()),
            "follow_up_semi_sekret": "sekret123",
            "hashed_email": "hashed123",
        }

    def test_a_file_over_the_limit_is_refused(self):
        form = FollowUpForm(
            data=self._base(),
            files={
                "followup_documents": _upload(
                    "x.bin", upload_limits.MAX_UPLOAD_FILE_BYTES + 1
                )
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("followup_documents", form.errors)

    def test_any_format_within_the_limit_is_accepted(self):
        # Nothing parses follow-up documents, so format is not checked.
        form = FollowUpForm(
            data=self._base(),
            files={"followup_documents": _upload("evidence.bin", 100)},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_too_many_files_are_refused(self):
        form = FollowUpForm(
            data=self._base(),
            files={
                "followup_documents": [
                    _upload(f"f{i}.bin", 10)
                    for i in range(upload_limits.MAX_UPLOAD_FILES + 1)
                ]
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("followup_documents", form.errors)


class DenialCollectedViewStoresNothingOversizedTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
        )

    def tearDown(self):
        for doc in PlanDocuments.objects.all():
            if doc.plan_document_enc:
                doc.plan_document_enc.delete(save=False)

    def _post(self, files):
        return self.client.post(
            reverse("dvc"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "plan_documents": files,
            },
        )

    def test_an_oversized_plan_document_is_not_stored(self):
        # The per-file cap the validator reads at call time.
        with patch.object(upload_limits, "MAX_POLICY_DOCUMENT_BYTES", 100):
            response = self._post([_pdf(size=500)])
        # The form is invalid, so the step is re-rendered (200) and nothing
        # reached the shared volume.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PlanDocuments.objects.filter(denial=self.denial).count(), 0
        )

    def test_a_plan_document_within_the_limit_is_stored(self):
        # On success the flow renders the next step (entity extraction).
        response = self._post([_pdf(size=500)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PlanDocuments.objects.filter(denial=self.denial).count(), 1
        )


class ThePlanDocumentPageSaysItsLimitsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="Denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
        )

    def test_the_hint_names_the_count_and_the_size(self):
        response = self.client.get(back_link(self.client, "dvc", self.denial, EMAIL))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"up to {upload_limits.MAX_UPLOAD_FILES} files and "
            f"{upload_limits.megabytes(upload_limits.MAX_UPLOAD_FILE_BYTES)}MB each",
        )


class ServerOcrRefusesTooMuchTest(TestCase):
    def test_too_many_images_are_refused_before_reading(self):
        uploads = [_upload(f"p{i}.png", 10) for i in range(upload_limits.MAX_OCR_FILES + 1)]
        self.assertIsNotNone(views.OCRView.refusal(uploads))

    def test_an_oversized_image_is_refused_before_reading(self):
        uploads = [_upload("p.png", upload_limits.MAX_OCR_FILE_BYTES + 1)]
        self.assertIsNotNone(views.OCRView.refusal(uploads))

    def test_images_within_the_limits_are_allowed(self):
        self.assertIsNone(views.OCRView.refusal([_upload("p.png", 10)]))

    def test_the_view_does_not_read_an_upload_over_the_count(self):
        ticked = {name: "on" for name in __import__(
            "fighthealthinsurance.consent", fromlist=["SERVER_OCR_BOXES"]
        ).SERVER_OCR_BOXES}
        uploads = [_upload(f"p{i}.png", 10) for i in range(upload_limits.MAX_OCR_FILES + 1)]
        with patch.object(views.OCRView, "_ocr") as ocr:
            response = self.client.post(
                reverse("server_side_ocr"), {"uploader": uploads, **ticked}
            )
        ocr.assert_not_called()
        self.assertContains(response, "Too many files")

    def test_the_page_says_the_image_limits(self):
        response = self.client.get(reverse("server_side_ocr"))
        self.assertContains(
            response,
            f"up to {upload_limits.MAX_OCR_FILES} at a time and "
            f"{upload_limits.megabytes(upload_limits.MAX_OCR_FILE_BYTES)}MB each",
        )


class SaveTimeoutScalesWithSizeTest(TestCase):
    def test_a_larger_file_gets_a_longer_write_window(self):
        class Sized:
            def __init__(self, n):
                self.size = n

        small = save_timeout_seconds(Sized(0))
        big = save_timeout_seconds(Sized(60 * 1024 * 1024))
        self.assertGreater(big, small)
        # A sizeless stream still gets the base window.
        self.assertEqual(save_timeout_seconds(object()), small)

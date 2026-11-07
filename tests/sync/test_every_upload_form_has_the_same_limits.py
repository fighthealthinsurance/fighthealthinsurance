"""Every page that takes files counts them the same way (upload_rate_limit).

One address, email or account may upload so many files an hour and a day,
across Explain My Policy, the appeal's plan documents step, follow-up
documents, server-side OCR, appeal attachments and the share page. Files are
counted when they arrive, before any check, and a refused upload counts
nothing and stores nothing. Logged-in staff are not counted. Every policy
or plan document upload also turns away a file too small to be one.
"""

import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import upload_limits, upload_rate_limit
from fighthealthinsurance.chat_forms import UnderstandPolicyForm
from fighthealthinsurance.forms import PlanDocumentsForm
from fighthealthinsurance.models import (
    Denial,
    FollowUpDocuments,
    PlanDocuments,
    PolicyDocument,
    UploadCount,
)
from fighthealthinsurance.shared_plan_documents import render_token

ADDRESS = "203.0.113.7"
OTHER_ADDRESS = "198.51.100.9"
EMAIL = "mary.walker@test-fhi.com"
HEADERS = {"HTTP_CF_CONNECTING_IP": ADDRESS}
REFUSED = "uploaded a lot of files in a short time"
PDF = b"%PDF-1.4 Summary of Benefits, Walker Mutual" + b" x" * 600


def pdf(name="SBC.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def visitor(address=ADDRESS, user=None):
    request = RequestFactory().post("/", HTTP_CF_CONNECTING_IP=address)
    if user is not None:
        request.user = user
    return request


@override_settings(FHI_UPLOAD_FILES_PER_HOUR=3, FHI_UPLOAD_FILES_PER_DAY=5)
class TheCountTestCase(TestCase):
    def test_files_under_the_hours_limit_are_taken(self):
        self.assertTrue(upload_rate_limit.take(visitor(), 3))

    def test_files_over_the_hours_limit_are_refused(self):
        upload_rate_limit.take(visitor(), 2)
        self.assertFalse(upload_rate_limit.take(visitor(), 2))

    def test_a_refused_upload_counts_nothing(self):
        upload_rate_limit.take(visitor(), 2)
        upload_rate_limit.take(visitor(), 2)
        self.assertTrue(upload_rate_limit.take(visitor(), 1))

    def test_the_days_limit_holds_across_hours(self):
        with patch("fighthealthinsurance.upload_rate_limit.files_per_hour") as hour:
            hour.return_value = 100
            upload_rate_limit.take(visitor(), 5)
        self.assertFalse(upload_rate_limit.take(visitor(), 1))

    def test_the_same_email_from_another_address_is_counted_together(self):
        upload_rate_limit.take(visitor(), 3, EMAIL)
        self.assertFalse(upload_rate_limit.take(visitor(OTHER_ADDRESS), 1, EMAIL))

    def test_another_address_without_the_email_has_its_own_count(self):
        upload_rate_limit.take(visitor(), 3, EMAIL)
        self.assertTrue(upload_rate_limit.take(visitor(OTHER_ADDRESS), 1))

    def test_an_email_is_counted_whatever_its_case(self):
        upload_rate_limit.take(visitor(), 3, EMAIL.upper())
        self.assertFalse(upload_rate_limit.take(visitor(OTHER_ADDRESS), 1, EMAIL))

    def test_staff_are_not_counted(self):
        staff = get_user_model().objects.create_user(
            "staff-uploads", password="unused-in-this-test", is_staff=True
        )
        upload_rate_limit.take(visitor(user=staff), 3)
        self.assertTrue(upload_rate_limit.take(visitor(user=staff), 3))

    def test_no_address_or_email_is_kept_as_written(self):
        upload_rate_limit.take(visitor(), 1, EMAIL)
        keys = " ".join(UploadCount.objects.values_list("key", flat=True))
        self.assertNotIn(ADDRESS, keys)

    def test_an_upload_goes_ahead_when_the_count_cannot_be_read(self):
        with patch.object(UploadCount.objects, "get_or_create", side_effect=OSError):
            self.assertTrue(upload_rate_limit.take(visitor(), 99))


def _use_up_the_hour():
    upload_rate_limit.take(visitor(), 3)


@override_settings(FHI_UPLOAD_FILES_PER_HOUR=3, FHI_UPLOAD_FILES_PER_DAY=10)
class EveryPageRefusesPastTheLimitTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        _use_up_the_hour()

    def test_explain_my_policy(self):
        response = self.client.post(
            reverse("understand_policy"),
            {"policy_document": pdf(), "email": EMAIL},
            **HEADERS,
        )
        self.assertContains(response, REFUSED)
        self.assertFalse(PolicyDocument.objects.exists())

    def test_the_appeal_plan_documents_step(self):
        denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret="sekret-for-upload-limits",
        )
        response = self.client.post(
            reverse("dvc"),
            {
                "denial_id": denial.denial_id,
                "email": EMAIL,
                "semi_sekret": "sekret-for-upload-limits",
                "plan_documents": [pdf()],
            },
            **HEADERS,
        )
        self.assertContains(response, REFUSED)
        self.assertFalse(PlanDocuments.objects.exists())

    def test_follow_up_documents(self):
        denial = Denial.objects.create(
            denial_text="Some denial text.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            raw_email=EMAIL,
        )
        path = (
            f"/v0/followup/{denial.uuid}/"
            f"{denial.hashed_email}/{denial.follow_up_semi_sekret}"
        )
        response = self.client.post(
            path, {"followup_documents": [pdf("notes.pdf")]}, **HEADERS
        )
        self.assertContains(response, REFUSED)
        self.assertFalse(FollowUpDocuments.objects.exists())

    def test_server_side_ocr(self):
        image = SimpleUploadedFile("page.png", b"\x89PNG" + b"0" * 300)
        with patch("fighthealthinsurance.views.OCRView._ocr") as ocr:
            response = self.client.post(
                reverse("server_side_ocr"),
                {"privacy": "on", "tos": "on", "personalonly": "on", "uploader": image},
                **HEADERS,
            )
        self.assertContains(response, REFUSED)
        ocr.assert_not_called()

    def test_the_share_page(self):
        response = self.client.post(
            reverse("share_plan_documents"),
            {
                "plan_documents": [pdf()],
                "agree": "on",
                "rendered": render_token(time.time() - 60),
            },
            **HEADERS,
        )
        self.assertContains(response, REFUSED)
        self.assertFalse(PolicyDocument.objects.exists())

    @override_settings(FIGHT_PAPERWORK_ENABLED=True)
    def test_appeal_attachments(self):
        user = get_user_model().objects.create_user(
            "attachments-uploader", password="unused-in-this-test"
        )
        # From a third address: the page's own address is already used up.
        self.assertTrue(upload_rate_limit.take(visitor("192.0.2.44", user=user), 3))
        self.client.force_login(user)
        response = self.client.post(
            reverse("appeal_attachments-list"),
            {"appeal_id": 1, "file": pdf("letter.pdf")},
            **{"HTTP_CF_CONNECTING_IP": OTHER_ADDRESS},
        )
        self.assertEqual(response.status_code, 429)


class RefusedOCRUploadsStillCountTestCase(TestCase):
    def test_an_ocr_upload_refused_for_its_size_still_counts(self):
        image = SimpleUploadedFile("page.png", b"\x89PNG" + b"0" * 300)
        with patch(
            "fighthealthinsurance.views.OCRView.refusal", return_value="Too big"
        ):
            response = Client().post(
                reverse("server_side_ocr"),
                {"privacy": "on", "tos": "on", "personalonly": "on", "uploader": image},
                **HEADERS,
            )
        self.assertContains(response, "Too big")
        self.assertTrue(UploadCount.objects.filter(count__gt=0).exists())


class PolicyDocumentsHaveAFloorTestCase(TestCase):
    def test_explain_my_policy_turns_away_a_line_of_text(self):
        form = UnderstandPolicyForm(
            data={}, files={"policy_document": SimpleUploadedFile("a.txt", b"g")}
        )
        form.is_valid()
        self.assertIn(
            upload_limits.TOO_SMALL_DOCUMENT_MESSAGE,
            form.errors.get("policy_document", []),
        )

    def test_the_appeal_step_turns_away_a_line_of_text(self):
        form = PlanDocumentsForm(
            data={}, files={"plan_documents": [SimpleUploadedFile("a.txt", b"g")]}
        )
        form.is_valid()
        self.assertTrue(
            any(
                upload_limits.TOO_SMALL_DOCUMENT_MESSAGE in message
                for message in form.errors.get("plan_documents", [])
            )
        )

    def test_a_full_document_passes_the_floor(self):
        upload_limits.validate_policy_document(pdf())


class DocumentsAndPhotosOnlyTestCase(TestCase):
    def test_follow_up_documents_turn_away_a_web_page(self):
        from fighthealthinsurance.forms import FollowUpForm

        form = FollowUpForm(
            data={},
            files={
                "followup_documents": [
                    SimpleUploadedFile("notes.html", b"<script>x</script>" * 20)
                ]
            },
        )
        form.is_valid()
        self.assertIn("followup_documents", form.errors)

    def test_an_attachment_named_pdf_must_be_a_pdf(self):
        from django import forms

        with self.assertRaises(forms.ValidationError):
            upload_limits.validate_attachment(
                SimpleUploadedFile("letter.pdf", b"MZ not a pdf" * 30)
            )

    def test_a_saved_email_is_turned_away_with_what_to_send_instead(self):
        from django import forms

        with self.assertRaises(forms.ValidationError) as caught:
            upload_limits.validate_attachment(
                SimpleUploadedFile("denial.eml", b"From: plan\n" * 40)
            )
        self.assertIn("screenshot or print it to PDF", caught.exception.messages[0])

    def test_a_spreadsheet_is_turned_away_with_what_to_send_instead(self):
        from django import forms

        with self.assertRaises(forms.ValidationError) as caught:
            upload_limits.validate_attachment(
                SimpleUploadedFile("bill.xlsx", b"PK\x03\x04" + b"0" * 400)
            )
        self.assertIn("save or print it as a PDF", caught.exception.messages[0])

    def test_an_unknown_type_gets_the_plain_message(self):
        self.assertEqual(
            upload_limits.unsupported_attachment_message(".xyz"),
            "Unsupported file type. Please upload a document (PDF, Word or "
            "text) or a photo (PNG, JPEG, GIF, TIFF, HEIC or WebP).",
        )

    def test_a_zip_named_docx_is_not_taken_as_an_attachment(self):
        import io
        import zipfile

        from django import forms

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes.txt", "not a Word file " * 30)
        with self.assertRaises(forms.ValidationError):
            upload_limits.validate_attachment(
                SimpleUploadedFile("letter.docx", buffer.getvalue())
            )

    def test_a_word_file_is_taken_as_an_attachment(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<w:document/>" * 30)
        upload_limits.validate_attachment(
            SimpleUploadedFile("letter.docx", buffer.getvalue())
        )

    def test_a_photo_of_a_letter_is_taken(self):
        upload_limits.validate_attachment(
            SimpleUploadedFile("letter.jpg", b"\xff\xd8\xff\xe0" + b"0" * 400)
        )


class FileNamesAreMadeSafeTestCase(TestCase):
    def test_folders_are_dropped(self):
        self.assertEqual(upload_limits.safe_filename("../../etc/passwd.txt"), "passwd.txt")

    def test_markup_characters_are_replaced(self):
        name = upload_limits.safe_filename('<img src=x onerror="a()">.pdf')
        self.assertNotIn("<", name)
        self.assertTrue(name.endswith(".pdf"))

    def test_a_name_that_is_only_an_extension_keeps_it(self):
        self.assertEqual(upload_limits.safe_filename(".pdf"), "file.pdf")

    def test_a_trailing_dot_is_dropped(self):
        self.assertEqual(upload_limits.safe_filename("letter."), "letter")

    def test_one_upload_over_the_hour_is_told_to_send_fewer(self):
        with override_settings(FHI_UPLOAD_FILES_PER_HOUR=20):
            self.assertEqual(
                upload_rate_limit.refusal_message(21), upload_rate_limit.BATCH_MESSAGE
            )
            self.assertEqual(
                upload_rate_limit.refusal_message(5), upload_rate_limit.LIMIT_MESSAGE
            )

    def test_a_long_name_is_cut_with_its_extension_kept(self):
        name = upload_limits.safe_filename("a" * 500 + ".PDF")
        self.assertEqual(len(name), upload_limits.MAX_FILENAME_CHARS)
        self.assertTrue(name.endswith(".pdf"))

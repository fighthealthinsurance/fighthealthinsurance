"""/server_side_ocr asks for the intake page's boxes, in its words, and the
server reads no upload until they are ticked."""

from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import consent, views
from fighthealthinsurance.forms import DenialForm, ServerOCRForm
from tests.sync.test_consent_record import _label_text

TICKED = {name: "on" for name in consent.SERVER_OCR_BOXES}
LETTER = "Your MRI was denied."


def _upload() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "letter.png", b"not read: _ocr is patched", content_type="image/png"
    )


class TheBoxesComeBeforeTheUploadIsReadTest(TestCase):
    def _post(self, data):
        with patch.object(views.OCRView, "_ocr", return_value=LETTER) as ocr:
            response = self.client.post(
                reverse("server_side_ocr"), {"uploader": _upload(), **data}
            )
        return response, ocr

    def test_an_upload_without_the_boxes_is_not_read(self):
        response, ocr = self._post({})
        ocr.assert_not_called()
        soup = BeautifulSoup(response.content.decode(), "html.parser")
        self.assertIsNone(soup.find(id="denial_text"))
        self.assertEqual(
            soup.find(id="agree_chk_error").get_text(),
            "Please tick each box above to go on.",
        )
        self.assertNotIn(consent.SERVER_OCR_SESSION_KEY, self.client.session)

    def test_an_upload_missing_any_one_box_is_not_read(self):
        for missing in consent.SERVER_OCR_BOXES:
            with self.subTest(missing=missing):
                data = {name: "on" for name in TICKED if name != missing}
                response, ocr = self._post(data)
                ocr.assert_not_called()
                soup = BeautifulSoup(response.content.decode(), "html.parser")
                self.assertIsNone(soup.find(id="denial_text"))
                self.assertEqual(soup.find(id=missing).get("aria-invalid"), "true")

    def test_an_upload_with_the_boxes_is_read(self):
        response, ocr = self._post(TICKED)
        ocr.assert_called_once()
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content.decode(), "html.parser")
        self.assertEqual(soup.find(id="denial_text").get_text(), LETTER)

    def test_the_boxes_are_kept_with_the_policy_versions(self):
        self._post(TICKED)
        note = self.client.session[consent.SERVER_OCR_SESSION_KEY]
        self.assertEqual(note["terms_version"], consent.TERMS_VERSION.isoformat())
        self.assertEqual(note["privacy_version"], consent.PRIVACY_VERSION.isoformat())
        self.assertEqual(
            note["boxes"],
            [
                {"name": name, "label": consent.BOXES[name], "ticked": True}
                for name in consent.SERVER_OCR_BOXES
            ],
        )
        self.assertTrue(note["accepted_at"])

    def test_a_missing_file_sends_the_boxes_back_ticked(self):
        with patch.object(views.OCRView, "_ocr") as ocr:
            response = self.client.post(reverse("server_side_ocr"), TICKED)
        ocr.assert_not_called()
        soup = BeautifulSoup(response.content.decode(), "html.parser")
        self.assertIn("Missing file", soup.get_text())
        for name in consent.SERVER_OCR_BOXES:
            with self.subTest(box=name):
                self.assertIn("checked", soup.find(id=name).attrs)


class TheUploadPageShowsTheIntakeBoxesTest(TestCase):
    def test_the_page_shows_the_words_the_record_keeps(self):
        page = self.client.get(reverse("server_side_ocr")).content.decode()
        for name in consent.SERVER_OCR_BOXES:
            with self.subTest(box=name):
                self.assertEqual(_label_text(page, name), consent.BOXES[name])

    def test_the_page_asks_for_each_box_before_it_sends_the_file(self):
        soup = BeautifulSoup(
            self.client.get(reverse("server_side_ocr")).content.decode(),
            "html.parser",
        )
        for name in consent.SERVER_OCR_BOXES:
            with self.subTest(box=name):
                box = soup.find(id=name)
                self.assertIn("required", box.attrs)
                self.assertNotIn("checked", box.attrs)

    def test_the_form_checks_the_intake_forms_own_fields(self):
        intake = DenialForm().fields
        fields = ServerOCRForm().fields
        self.assertEqual(list(fields), list(consent.SERVER_OCR_BOXES))
        for name in consent.SERVER_OCR_BOXES:
            with self.subTest(box=name):
                self.assertTrue(fields[name].required)
                self.assertEqual(
                    fields[name].error_messages, intake[name].error_messages
                )

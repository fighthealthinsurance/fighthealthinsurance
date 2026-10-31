"""The appeal's plan documents step says what is wrong with a refused upload.

The step checks each file before anything is stored, as every plan document
upload does (upload_limits.validate_uploads with validate_policy_document;
tests/sync/test_large_file_handling.py pins the limits themselves). A refused
upload comes back to the page with nothing stored and each refused file
named, under the box (one to a line) and in the list at the top, in the
person's words rather than the field's name in the code. The box is
described by its limits and its error, so a screen reader reads them with
it. The picker offers only the types the check takes, and a file that
passes is stored whole, its shared copy too.
"""

from bs4 import BeautifulSoup
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import upload_limits
from fighthealthinsurance.forms import PlanDocumentsForm
from fighthealthinsurance.ml.ml_document_extraction import read_and_decrypt_file
from fighthealthinsurance.models import Denial, PlanDocuments, PolicyDocument

EMAIL = "rebecca.crumpler@test-fhi.com"
SEMI_SEKRET = "sekret-for-the-plan-documents-step"
PDF = b"%PDF-1.4 Summary of Benefits for the Crumpler Cooperative" + b" x" * 100
TXT = b"Summary Plan Description, Crumpler Cooperative, plan year 2026."


def pdf(name="SBC.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def txt(name="SPD.txt", content=TXT):
    return SimpleUploadedFile(name, content, content_type="text/plain")


def docx(name="EOC.docx"):
    # A zip's first bytes are what the step checks for a Word file.
    return SimpleUploadedFile(name, b"PK\x03\x04 word document", content_type="")


class _StepTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
        )

    def tearDown(self):
        # Rows roll back with the test; their stored files would not.
        for doc in PlanDocuments.objects.all():
            if doc.plan_document_enc:
                doc.plan_document_enc.delete(save=False)
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def post(self, files, **extra):
        data = {
            "denial_id": self.denial.denial_id,
            "email": EMAIL,
            "semi_sekret": SEMI_SEKRET,
            "plan_documents": files,
        }
        data.update(extra)
        return self.client.post(reverse("dvc"), data)

    def stored(self):
        return PlanDocuments.objects.filter(denial=self.denial).count()

    def soup(self, response):
        return BeautifulSoup(response.content, "html.parser")

    def assertRefused(self, response, *said):
        """The step came back with nothing stored and said each of ``said``,
        under the box and in the list at the top, never the field's name."""
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "plan_documents.html")
        self.assertEqual(self.stored(), 0)
        page = self.soup(response)
        under_the_box = page.find(id="plan_documents_error").get_text(" ", strip=True)
        at_the_top = page.find(id="step-errors").get_text(" ", strip=True)
        for words in said:
            with self.subTest(words=words):
                self.assertIn(words, under_the_box)
                self.assertIn(words, at_the_top)
        self.assertIsNone(page.find(class_="errorlist"), "form.errors is dumped")
        self.assertNotIn("plan_documents", at_the_top)


class FilesTheAppealCanReadAreStoredTest(_StepTestCase):
    def test_a_pdf_a_word_file_and_a_text_file_are_stored(self):
        response = self.post([pdf(), docx(), txt()])
        self.assertTemplateUsed(response, "entity_extract.html")
        self.assertEqual(self.stored(), 3)

    def test_a_shared_copy_is_still_whole_after_the_check(self):
        # The check reads each file's first bytes and rewinds it, so the
        # appeal's save and the shared copy both get the whole file.
        self.post([pdf()], share_for_research="on")
        self.assertEqual(self.stored(), 1)
        copy = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_SHARED_APPEAL)
        self.assertEqual(read_and_decrypt_file(copy.document_enc), PDF)


class ARefusedUploadSaysWhyTest(_StepTestCase):
    def test_a_file_type_the_reader_has_no_parser_for(self):
        scan = SimpleUploadedFile("Crumpler scan.jpg", b"\xff\xd8\xff" + b"x" * 50)
        self.assertRefused(
            self.post([scan]), "Crumpler scan.jpg: Unsupported file type"
        )

    def test_a_pdf_that_is_not_one(self):
        self.assertRefused(
            self.post([pdf("notes.pdf", b"just some notes")]),
            "notes.pdf: The file does not appear to be a valid PDF.",
        )

    def test_a_file_over_the_limit(self):
        big = pdf("EOC.pdf", b"%PDF-1.4 " + b"x" * upload_limits.MAX_UPLOAD_FILE_BYTES)
        self.assertRefused(
            self.post([big]), "EOC.pdf: File size must be less than 20MB"
        )

    def test_one_bad_file_stops_the_whole_upload(self):
        scan = SimpleUploadedFile("scan.heic", b"\x00\x00\x00 ftypheic")
        self.assertRefused(self.post([pdf(), scan, txt()]), "scan.heic:")

    def test_every_bad_file_is_named(self):
        self.assertRefused(
            self.post([pdf("a.pdf", b"not a pdf"), pdf("b.pdf", b"nor this")]),
            "a.pdf:",
            "b.pdf:",
        )

    def test_each_refused_file_has_a_line_of_its_own_under_the_box(self):
        response = self.post([pdf("a.pdf", b"not a pdf"), pdf("b.pdf", b"nor this")])
        note = self.soup(response).find(id="plan_documents_error")
        self.assertEqual(
            list(note.stripped_strings),
            [
                "a.pdf: The file does not appear to be a valid PDF.",
                "b.pdf: The file does not appear to be a valid PDF.",
            ],
        )
        self.assertEqual(len(note.find_all("br")), 1)

    def test_too_many_files(self):
        files = [
            txt(f"part-{n}.txt") for n in range(upload_limits.MAX_UPLOAD_FILES + 1)
        ]
        self.assertRefused(self.post(files), upload_limits.too_many_files_message())

    def test_a_hidden_fields_error_is_named_after_its_label(self):
        # No email: the form is invalid before any file is looked at.
        response = self.client.post(
            reverse("dvc"),
            {"denial_id": self.denial.denial_id, "semi_sekret": SEMI_SEKRET},
        )
        page = self.soup(response)
        errors = page.find(id="step-errors").get_text(" ", strip=True)
        self.assertIn("Email: This field is required.", errors)
        self.assertIsNone(page.find(class_="errorlist"))


class ThePickerOffersWhatTheCheckTakesTest(_StepTestCase):
    def page(self):
        # The way in from the health history step renders the page.
        response = self.client.post(
            reverse("hh"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": "",
            },
        )
        self.assertTemplateUsed(response, "plan_documents.html")
        return self.soup(response)

    def test_the_input_takes_several_files_of_the_checked_types(self):
        box = self.page().find("input", attrs={"name": "plan_documents"})
        self.assertEqual(
            set(box["accept"].split(",")), upload_limits.SUPPORTED_POLICY_EXTENSIONS
        )
        self.assertIn("multiple", box.attrs)
        self.assertEqual(box["id"], "plan_documents")

    def test_the_input_is_described_by_its_limits_and_its_error(self):
        page = self.page()
        box = page.find("input", attrs={"name": "plan_documents"})
        described_by = box["aria-describedby"].split()
        self.assertEqual(
            described_by, ["plan_documents_limits", "plan_documents_error"]
        )
        for element_id in described_by:
            self.assertIsNotNone(page.find(id=element_id), element_id)

    def test_the_form_field_carries_the_same_types(self):
        field = BeautifulSoup(
            str(PlanDocumentsForm()["plan_documents"]), "html.parser"
        ).find("input")
        self.assertEqual(field["accept"], PlanDocumentsForm.ACCEPT)

"""The admin and staff pages of the public plan-document corpus: the
PublicPlanDocument admin and its parsed upload, the read-only
PlanDocumentMatch admin, the staff text view and the Experiments entry.

Public data: the documents here are invented and hold no PHI.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    InsurancePlan,
    PlanDocumentMatch,
    PublicPlanDocument,
)
from fighthealthinsurance.upload_limits import MAX_POLICY_DOCUMENT_BYTES
from tests.pdf_fixtures import make_pdf_bytes

User = get_user_model()
EMAIL = "rebecca.crumpler@example.com"
EOC_KEY = "kaiser-norcal-eoc-2025-hmo"
SBC_KEY = "kaiser-norcal-sbc-2025-hmo"
TEXT = (
    "[Page 1]\nNorthern California member benefits.\n\n"
    "[Page 2]\nAppeals and grievances."
)
PDF_PAGES = [
    "Northern California Summary of Benefits and Coverage.",
    "Covered services and what you pay.",
]
SOCAL_KEY = "kaiser-socal-eoc-2025-hmo"
MATCH_REGEX = r"\bdeductible\s+hmo\b|\bkp\s*select\b"
NEGATIVE_REGEX = r"\bsouthern\s+california\b|\bmedi[-\s]cal\b"


class _CorpusAdminTestCase(TestCase):
    """A superuser's client (the pattern of tests/sync/test_admin.py) and a
    carrier to hang documents on."""

    def setUp(self):
        super().setUp()
        User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )
        self.client.login(username="admin", password="adminpass123")
        self.kaiser = InsuranceCompany.objects.create(
            name="Kaiser Permanente",
            alt_names="Kaiser\nKP",
            regex=r"kaiser\s*permanente|kaiser(?!\s*foundation)",
        )

    def document(self, **overrides):
        fields = dict(
            document_key=EOC_KEY,
            insurance_company=self.kaiser,
            title="Kaiser Permanente Northern California 2025 HMO Evidence of Coverage",
            document_type=PublicPlanDocument.DOCUMENT_TYPE_EVIDENCE_OF_COVERAGE,
            line_of_business="commercial",
            state="CA",
            region="Northern California",
            plan_year=2025,
            match_keywords=["northern california", "san francisco"],
            text=TEXT,
            text_sha256=PublicPlanDocument.text_hash(TEXT),
            page_count=2,
            char_count=len(TEXT),
        )
        fields.update(overrides)
        return PublicPlanDocument.objects.create(**fields)

    def match(self, document):
        denial, _ = Denial.objects.get_or_create(
            denial_id=1,
            defaults=dict(
                semi_sekret="sekret", hashed_email=Denial.get_hashed_email(EMAIL)
            ),
        )
        return PlanDocumentMatch.objects.create(
            denial=denial,
            document=document,
            company=self.kaiser,
            score=49.0,
            signals={"keyword": 20, "keyword_hits": 2, "state": 10, "identifying": 1},
            rank=1,
            selected=True,
            inputs_hash="0" * 64,
        )

    def admin_url(self, model, page, *args):
        return reverse(f"admin:fighthealthinsurance_{model}_{page}", args=args)

    def form_data(self, upload=None, **overrides):
        """Every field the add and change forms render (admin.py's fieldsets,
        less the read-only ones), for the SBC: JSON fields post as JSON text,
        a RegexField as its pattern, and the date-time as its two halves.
        ``overrides`` replace values; ``upload`` is sent only when given."""
        data = {
            "document_key": SBC_KEY,
            "insurance_company": str(self.kaiser.pk),
            "insurance_plan": "",
            "title": "Kaiser Permanente Northern California 2025 HMO Summary of Benefits",
            "document_type": "summary_of_benefits",
            "coverage_kind": "medical",
            "line_of_business": "commercial",
            "is_active": "on",
            "notes": "",
            "state": "CA",
            "region": "Northern California",
            "employer_name": "",
            "plan_year": "2025",
            "plan_id_prefixes": "[]",
            "match_keywords": '["northern california", "san francisco"]',
            "match_regex": "",
            "negative_regex": r"\bsouthern\s+california\b",
            "source_url": "https://healthy.kaiserpermanente.org/norcal-sbc-2025.pdf",
            "fetched_at_0": "",
            "fetched_at_1": "",
            "_save": "Save",
        }
        data.update(overrides)
        if upload is not None:
            data["upload"] = upload
        return data

    def form_errors(self, response):
        if response.status_code == 302 or response.context is None:
            return ""
        return str(response.context["adminform"].form.errors)

    def three_documents(self):
        """Three Kaiser documents, each with its own key and title."""
        return [
            self.document(document_key=key, title=title)
            for key, title in (
                (
                    EOC_KEY,
                    "Kaiser Permanente Northern California 2025 HMO Evidence of Coverage",
                ),
                (
                    SBC_KEY,
                    "Kaiser Permanente Northern California 2025 HMO Summary of Benefits",
                ),
                (
                    SOCAL_KEY,
                    "Kaiser Permanente Southern California 2025 HMO Evidence of Coverage",
                ),
            )
        ]


class CorpusAdminPagesTest(_CorpusAdminTestCase):
    def test_public_plan_document_changelist_lists_the_documents(self):
        doc = self.document()
        response = self.client.get(self.admin_url("publicplandocument", "changelist"))
        self.assertContains(response, doc.document_key)

    def test_plan_document_match_changelist_lists_the_matches(self):
        self.match(self.document())
        response = self.client.get(self.admin_url("plandocumentmatch", "changelist"))
        self.assertContains(
            response,
            "Kaiser Permanente: Kaiser Permanente Northern California 2025 HMO "
            "Evidence of Coverage",
        )

    def test_public_plan_document_changelist_lists_several_documents(self):
        docs = self.three_documents()
        response = self.client.get(self.admin_url("publicplandocument", "changelist"))
        self.assertEqual(response.status_code, 200)
        for doc in docs:
            self.assertContains(response, doc.document_key)

    def test_plan_document_match_changelist_lists_several_matches(self):
        docs = self.three_documents()
        for doc in docs:
            self.match(doc)
        response = self.client.get(self.admin_url("plandocumentmatch", "changelist"))
        self.assertEqual(response.status_code, 200)
        for doc in docs:
            self.assertContains(response, f"Kaiser Permanente: {doc.title}")

    def test_public_plan_document_change_form_renders_with_a_link_to_the_text(self):
        doc = self.document()
        response = self.client.get(self.admin_url("publicplandocument", "change", doc.pk))
        self.assertContains(
            response, reverse("staff_plan_document_corpus_text", kwargs={"pk": doc.pk})
        )

    def test_plan_document_match_add_view_is_forbidden(self):
        response = self.client.get(self.admin_url("plandocumentmatch", "add"))
        self.assertEqual(response.status_code, 403)


class PublicPlanDocumentAddFormTest(_CorpusAdminTestCase):
    def post_add(self, upload):
        return self.client.post(
            self.admin_url("publicplandocument", "add"), self.form_data(upload)
        )

    def pdf_upload(self):
        return SimpleUploadedFile(
            "norcal-sbc-2025.pdf", make_pdf_bytes(PDF_PAGES), content_type="application/pdf"
        )

    def added(self):
        return PublicPlanDocument.objects.get(document_key=SBC_KEY)

    def test_a_pdf_upload_creates_a_row_with_the_parsed_text(self):
        response = self.post_add(self.pdf_upload())
        self.assertEqual(response.status_code, 302, self.form_errors(response))
        doc = self.added()
        self.assertTrue(doc.text.startswith("[Page 1]\n"), doc.text[:40])
        self.assertIn(PDF_PAGES[0], doc.text)
        self.assertIn(f"[Page 2]\n{PDF_PAGES[1]}", doc.text)
        self.assertEqual(doc.page_count, 2)

    def test_the_added_row_carries_hash_counts_and_the_admin_batch(self):
        self.post_add(self.pdf_upload())
        doc = self.added()
        self.assertEqual(doc.text_sha256, PublicPlanDocument.text_hash(doc.text))
        self.assertEqual(doc.char_count, len(doc.text))
        self.assertEqual(doc.import_batch, "admin")

    def test_the_added_rows_matching_fields_come_from_the_form(self):
        self.post_add(self.pdf_upload())
        doc = self.added()
        self.assertEqual(doc.insurance_company, self.kaiser)
        self.assertEqual(doc.match_keywords, ["northern california", "san francisco"])
        self.assertEqual(doc.negative_regex.pattern, r"\bsouthern\s+california\b")
        self.assertEqual(
            (doc.state, doc.region, doc.plan_year), ("CA", "Northern California", 2025)
        )

    def test_an_unsupported_upload_is_rejected(self):
        response = self.post_add(
            SimpleUploadedFile(
                "setup.exe",
                b"MZ\x90\x00" + b"\x00" * 60,
                content_type="application/octet-stream",
            )
        )
        self.assertContains(response, "Unsupported file type")
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_a_file_that_only_pretends_to_be_a_pdf_is_rejected(self):
        response = self.post_add(
            SimpleUploadedFile(
                "sbc.pdf", b"not a pdf at all", content_type="application/pdf"
            )
        )
        self.assertContains(response, "does not appear to be a valid PDF")
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_an_add_without_an_upload_is_refused(self):
        response = self.client.post(
            self.admin_url("publicplandocument", "add"), self.form_data()
        )
        self.assertContains(response, "A new document needs its file.")
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_a_pdf_with_no_text_is_refused_with_the_reason(self):
        response = self.post_add(
            SimpleUploadedFile(
                "blank.pdf", make_pdf_bytes([""]), content_type="application/pdf"
            )
        )
        self.assertContains(response, "no text could be read")
        self.assertEqual(PublicPlanDocument.objects.count(), 0)

    def test_an_upload_over_the_size_limit_is_refused(self):
        header = b"%PDF-1.4\n"
        response = self.post_add(
            SimpleUploadedFile(
                "huge.pdf",
                header + b"\0" * (MAX_POLICY_DOCUMENT_BYTES + 1 - len(header)),
                content_type="application/pdf",
            )
        )
        self.assertContains(response, "File size must be less than 20MB")
        self.assertEqual(PublicPlanDocument.objects.count(), 0)


class PublicPlanDocumentChangeFormTest(_CorpusAdminTestCase):
    """The change form of a row with both regex fields set: a RegexField
    holds a compiled pattern, and the form shows and saves its text."""

    def setUp(self):
        super().setUp()
        self.doc = self.document(match_regex=MATCH_REGEX, negative_regex=NEGATIVE_REGEX)
        self.url = self.admin_url("publicplandocument", "change", self.doc.pk)

    def unchanged_post(self):
        """The form posted back as the row stands: form_data's SBC values
        replaced by this EOC row's own."""
        return self.form_data(
            document_key=self.doc.document_key,
            title=self.doc.title,
            document_type=self.doc.document_type,
            match_regex=MATCH_REGEX,
            negative_regex=NEGATIVE_REGEX,
            source_url="",
        )

    def test_shows_the_regex_patterns_as_their_text(self):
        response = self.client.get(self.url)
        self.assertContains(response, f'value="{MATCH_REGEX}"')
        self.assertContains(response, f'value="{NEGATIVE_REGEX}"')
        self.assertNotContains(response, "re.compile(")

    def test_saving_the_form_unchanged_keeps_the_regex_patterns(self):
        response = self.client.post(self.url, self.unchanged_post())
        self.assertEqual(response.status_code, 302, self.form_errors(response))
        self.doc.refresh_from_db()
        self.assertEqual(
            (self.doc.match_regex.pattern, self.doc.negative_regex.pattern),
            (MATCH_REGEX, NEGATIVE_REGEX),
        )

    def test_saving_the_form_keeps_the_text_the_form_never_shows(self):
        # The admin loads the row without its text; saving must not blank it.
        response = self.client.post(self.url, self.unchanged_post())
        self.assertEqual(response.status_code, 302, self.form_errors(response))
        self.doc.refresh_from_db()
        self.assertEqual((self.doc.text, self.doc.page_count), (TEXT, 2))


class StaffTextViewTest(_CorpusAdminTestCase):
    def url(self, pk):
        return reverse("staff_plan_document_corpus_text", kwargs={"pk": pk})

    def test_serves_the_text_as_sandboxed_plain_text_to_staff(self):
        doc = self.document()
        response = self.client.get(self.url(doc.pk))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response["Content-Type"].startswith("text/plain"), response["Content-Type"]
        )
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertIn("[Page 2]\nAppeals and grievances.", response.content.decode())

    def test_redirects_anonymous_users_to_the_login_page(self):
        doc = self.document()
        self.client.logout()
        response = self.client.get(self.url(doc.pk))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_an_unknown_document_is_a_404(self):
        self.assertEqual(self.client.get(self.url(999_999)).status_code, 404)


class ExperimentsPageTest(TestCase):
    def test_lists_plan_document_matching(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        response = self.client.get("/timbit/help/experiments")
        self.assertContains(response, "Plan document matching")
        self.assertContains(response, "PLAN_DOCUMENT_MATCHING_ENABLED")


class ReviewFindingsAdminTest(_CorpusAdminTestCase):
    """The match admin is read-only all the way down, and the document form
    refuses a plan of another insurer and phrase lists that are not lists."""

    def test_a_match_cannot_be_deleted_from_the_admin(self):
        row = self.match(self.document())
        response = self.client.get(self.admin_url("plandocumentmatch", "delete", row.pk))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PlanDocumentMatch.objects.filter(pk=row.pk).exists())

    def change_post(self, doc, **overrides):
        return self.client.post(
            self.admin_url("publicplandocument", "change", doc.pk),
            self.form_data(
                document_key=doc.document_key,
                title=doc.title,
                document_type=doc.document_type,
                negative_regex="",
                source_url="",
                **overrides,
            ),
        )

    def test_a_plan_of_another_insurer_is_refused(self):
        aetna = InsuranceCompany.objects.create(name="Aetna", regex=r"aetna")
        plan = InsurancePlan.objects.create(insurance_company=aetna, plan_name="Aetna HMO")
        doc = self.document()
        response = self.change_post(doc, insurance_plan=str(plan.pk))
        self.assertIn("That plan belongs to another insurer.", self.form_errors(response))
        doc.refresh_from_db()
        self.assertIsNone(doc.insurance_plan_id)

    def test_a_phrase_list_given_as_a_string_is_refused(self):
        doc = self.document()
        response = self.change_post(doc, plan_id_prefixes='"GRP"')
        self.assertIn("A JSON list of strings", self.form_errors(response))
        doc.refresh_from_db()
        self.assertEqual(doc.plan_id_prefixes, [])

"""Tests for the Understand Your Policy feature."""

import io
import json
import uuid
from unittest.mock import MagicMock, patch

import pymupdf
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.chat_forms import (
    SUPPORTED_POLICY_CONTENT_TYPES,
    SUPPORTED_POLICY_EXTENSIONS,
    UnderstandPolicyForm,
)
from fighthealthinsurance.chat_interface import _detect_policy_analysis_request
from fighthealthinsurance.ml.ml_policy_doc_helper import MLPolicyDocHelper
from fighthealthinsurance.models import PolicyDocument, PolicyDocumentAnalysis

# --- Form Validation Tests ---


class UnderstandPolicyFormValidationTest(TestCase):
    """Tests for UnderstandPolicyForm file validation."""

    def _make_form_data(self, **overrides):
        """Helper to build base form data."""
        data = {
            "first_name": "Test",
            "last_name": "User",
            "email": "test@example.com",
            "document_type": "summary_of_benefits",
            "tos_agreement": True,
            "privacy_policy": True,
        }
        data.update(overrides)
        return data

    def _make_uploaded_file(self, name, content, content_type):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_accepts_pdf(self):
        pdf_content = b"%PDF-1.4 fake pdf content"
        f = self._make_uploaded_file("test.pdf", pdf_content, "application/pdf")
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_accepts_docx(self):
        docx_content = b"PK\x03\x04 fake docx content"
        f = self._make_uploaded_file(
            "test.docx",
            docx_content,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_accepts_txt(self):
        f = self._make_uploaded_file("test.txt", b"plain text content", "text/plain")
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_accepts_rtf(self):
        f = self._make_uploaded_file("test.rtf", b"rtf content here", "application/rtf")
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_rejects_exe(self):
        f = self._make_uploaded_file(
            "malware.exe", b"MZ\x90\x00 evil content", "application/x-msdownload"
        )
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("policy_document", form.errors)

    def test_rejects_html(self):
        f = self._make_uploaded_file("page.html", b"<html>hello</html>", "text/html")
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertFalse(form.is_valid())

    def test_rejects_jpg(self):
        f = self._make_uploaded_file(
            "image.jpg", b"\xff\xd8\xff\xe0 image data", "image/jpeg"
        )
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertFalse(form.is_valid())

    def test_rejects_fake_pdf_magic_bytes(self):
        """A .pdf with non-PDF magic bytes should be rejected."""
        f = self._make_uploaded_file(
            "fake.pdf", b"NOT-A-PDF content here", "application/pdf"
        )
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("policy_document", form.errors)

    def test_rejects_fake_docx_magic_bytes(self):
        """A .docx with non-ZIP magic bytes should be rejected."""
        f = self._make_uploaded_file(
            "fake.docx",
            b"NOT-A-DOCX content here",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertFalse(form.is_valid())

    def test_rejects_oversized_file(self):
        """Files > 20MB should be rejected."""
        big_content = b"%PDF-1.4 " + b"x" * (20 * 1024 * 1024 + 1)
        f = self._make_uploaded_file("big.pdf", big_content, "application/pdf")
        form = UnderstandPolicyForm(
            data=self._make_form_data(), files={"policy_document": f}
        )
        self.assertFalse(form.is_valid())

    def test_requires_tos_agreement(self):
        pdf_content = b"%PDF-1.4 fake pdf content"
        f = self._make_uploaded_file("test.pdf", pdf_content, "application/pdf")
        form = UnderstandPolicyForm(
            data=self._make_form_data(tos_agreement=False),
            files={"policy_document": f},
        )
        self.assertFalse(form.is_valid())

    def test_requires_email(self):
        pdf_content = b"%PDF-1.4 fake pdf content"
        f = self._make_uploaded_file("test.pdf", pdf_content, "application/pdf")
        form = UnderstandPolicyForm(
            data=self._make_form_data(email=""),
            files={"policy_document": f},
        )
        self.assertFalse(form.is_valid())

    def test_optional_user_question(self):
        pdf_content = b"%PDF-1.4 fake pdf content"
        f = self._make_uploaded_file("test.pdf", pdf_content, "application/pdf")
        form = UnderstandPolicyForm(
            data=self._make_form_data(user_question="Is my MRI covered?"),
            files={"policy_document": f},
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["user_question"], "Is my MRI covered?")


# --- Text Extraction Tests ---


class TextExtractionTest(TestCase):
    """Tests for MLPolicyDocHelper text extraction methods."""

    def test_extract_text_from_real_pdf(self):
        """Create a real PDF in memory and extract text."""
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "This is page 1 of the test policy.")
        pdf_bytes = doc.tobytes()
        doc.close()

        full_text, page_dict = MLPolicyDocHelper._extract_text_from_pdf_bytes(pdf_bytes)
        self.assertIn("page 1", full_text.lower())
        self.assertIn(1, page_dict)

    def test_extract_text_from_plaintext(self):
        content = b"This is a plain text policy document with coverage details."
        full_text, page_dict = MLPolicyDocHelper._extract_text_from_plaintext_bytes(
            content
        )
        self.assertIn("plain text policy", full_text)
        self.assertEqual(len(page_dict), 1)

    def test_extract_text_dispatcher_pdf(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Dispatcher PDF test.")
        pdf_bytes = doc.tobytes()
        doc.close()

        full_text, _ = MLPolicyDocHelper.extract_text_from_bytes(
            pdf_bytes, "policy.pdf"
        )
        self.assertIn("Dispatcher PDF test", full_text)

    def test_extract_text_dispatcher_txt(self):
        full_text, _ = MLPolicyDocHelper.extract_text_from_bytes(
            b"Hello world", "notes.txt"
        )
        self.assertIn("Hello world", full_text)

    def test_extract_text_dispatcher_unsupported(self):
        full_text, page_dict = MLPolicyDocHelper.extract_text_from_bytes(
            b"data", "file.xyz"
        )
        self.assertEqual(full_text, "")
        self.assertEqual(page_dict, {})

    def test_corrupt_pdf_returns_empty(self):
        full_text, page_dict = MLPolicyDocHelper._extract_text_from_pdf_bytes(
            b"not a pdf at all"
        )
        self.assertEqual(full_text, "")
        self.assertEqual(page_dict, {})


# --- JSON Parsing Tests ---


class AnalysisParsingTest(TestCase):
    """Tests for MLPolicyDocHelper._parse_analysis_response."""

    def test_valid_json(self):
        data = {
            "exclusions": [{"text": "No cosmetic surgery", "page": 5}],
            "inclusions": [{"text": "Preventive care", "page": 2}],
            "appeal_clauses": [{"text": "30-day appeal window", "page": 10}],
            "summary": "This is a test summary.",
            "quotable_sections": [{"quote": "All medically necessary", "page": 3}],
        }
        result = MLPolicyDocHelper._parse_analysis_response(json.dumps(data))
        self.assertIsNotNone(result)
        self.assertEqual(len(result["exclusions"]), 1)

    def test_json_in_markdown_code_block(self):
        data = {
            "exclusions": [],
            "inclusions": [],
            "appeal_clauses": [],
            "summary": "Summary text.",
        }
        response = f"Here is the analysis:\n```json\n{json.dumps(data)}\n```"
        result = MLPolicyDocHelper._parse_analysis_response(response)
        self.assertIsNotNone(result)

    def test_missing_required_keys_rejected(self):
        data = {"exclusions": [], "summary": "Only partial data."}
        result = MLPolicyDocHelper._parse_analysis_response(json.dumps(data))
        self.assertIsNone(result)

    def test_garbage_input_returns_none(self):
        result = MLPolicyDocHelper._parse_analysis_response("This is not JSON at all!")
        self.assertIsNone(result)

    def test_json_with_trailing_text(self):
        data = {
            "exclusions": [],
            "inclusions": [],
            "appeal_clauses": [],
            "summary": "OK",
        }
        response = json.dumps(data) + "\n\nSome trailing explanation text."
        result = MLPolicyDocHelper._parse_analysis_response(response)
        self.assertIsNotNone(result)


# --- Policy Analysis Detection Tests ---


class PolicyAnalysisDetectionTest(TestCase):
    """Tests for _detect_policy_analysis_request regex."""

    def test_matches_structured_form_message(self):
        msg = (
            "I've uploaded my insurance Summary of Benefits: my_policy.pdf\n\n"
            "Please analyze this document and help me understand:\n"
            "1. What is covered"
        )
        self.assertTrue(_detect_policy_analysis_request(msg))

    def test_matches_with_user_question(self):
        msg = (
            "I've uploaded my insurance Medical Policy: bcbs_policy.pdf\n\n"
            "My question: Is my MRI covered?\n\n"
            "Please analyze this document and help me understand:"
        )
        self.assertTrue(_detect_policy_analysis_request(msg))

    def test_no_false_positive_normal_chat(self):
        normal_messages = [
            "What does my denial letter mean?",
            "Can you explain the summary of benefits?",
            "I want to appeal my insurance denial.",
            "Tell me about what is covered under my plan.",
            "What are the exclusions in my policy?",
            "Help me understand my insurance.",
            "I uploaded a document earlier, can you help?",
        ]
        for msg in normal_messages:
            self.assertFalse(
                _detect_policy_analysis_request(msg),
                f"False positive on: {msg}",
            )


# --- View Tests ---


@override_settings(RECAPTCHA_TESTING=True)
class UnderstandPolicyViewTest(TestCase):
    """Tests for the UnderstandPolicyView."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("understand_policy")

    def test_get_returns_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Understand My Policy")
        self.assertContains(response, "policy_document")
        self.assertContains(response, "document_type")

    def test_post_without_file_shows_errors(self):
        response = self.client.post(
            self.url,
            {
                "first_name": "Test",
                "last_name": "User",
                "email": "test@example.com",
                "document_type": "summary_of_benefits",
                "tos_agreement": "on",
                "privacy_policy": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")

    def test_valid_pdf_upload_creates_document(self):
        pdf_content = b"%PDF-1.4 test policy content" + b"x" * 100
        response = self.client.post(
            self.url,
            {
                "first_name": "Test",
                "last_name": "User",
                "email": "test@example.com",
                "document_type": "summary_of_benefits",
                "tos_agreement": "on",
                "privacy_policy": "on",
                "policy_document": SimpleUploadedFile(
                    "policy.pdf", pdf_content, content_type="application/pdf"
                ),
            },
        )
        # Should redirect to chat via chat_redirect.html (rendered as 200)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 1)
        doc = PolicyDocument.objects.first()
        self.assertEqual(doc.document_type, "summary_of_benefits")
        self.assertIn("policy", doc.filename)

    def test_valid_upload_stores_session_data(self):
        pdf_content = b"%PDF-1.4 test policy content" + b"x" * 100
        self.client.post(
            self.url,
            {
                "first_name": "Test",
                "last_name": "User",
                "email": "test@example.com",
                "document_type": "medical_policy",
                "tos_agreement": "on",
                "privacy_policy": "on",
                "policy_document": SimpleUploadedFile(
                    "medical.pdf", pdf_content, content_type="application/pdf"
                ),
            },
        )
        session = self.client.session
        self.assertIn("policy_document_id", session)
        self.assertTrue(session.get("consent_completed"))


# --- Model Tests ---


class PolicyDocumentModelTest(TestCase):
    """Tests for PolicyDocument and PolicyDocumentAnalysis models."""

    def test_create_policy_document(self):
        doc = PolicyDocument.objects.create(
            document_type="summary_of_benefits",
            filename="test_policy.pdf",
            hashed_email="abc123",
            session_key="sess_123",
        )
        self.assertIsNotNone(doc.id)
        self.assertEqual(
            str(doc), "PolicyDocument: test_policy.pdf (summary_of_benefits)"
        )

    def test_create_policy_document_analysis(self):
        doc = PolicyDocument.objects.create(
            document_type="medical_policy",
            filename="medical.pdf",
        )
        analysis = PolicyDocumentAnalysis.objects.create(
            policy_document=doc,
            user_question="Is my surgery covered?",
            exclusions=[{"text": "No cosmetic surgery", "page": 5}],
            inclusions=[{"text": "Emergency care", "page": 2}],
            appeal_clauses=[
                {"text": "30 day window", "page": 10, "type": "appeal_deadline"}
            ],
            summary="This policy covers emergency care but excludes cosmetic procedures.",
            quotable_sections=[{"quote": "All medically necessary care", "page": 3}],
        )
        self.assertIsNotNone(analysis.id)
        self.assertEqual(analysis.policy_document, doc)

    def test_cascade_delete(self):
        doc = PolicyDocument.objects.create(
            document_type="other",
            filename="test.txt",
        )
        PolicyDocumentAnalysis.objects.create(
            policy_document=doc,
            summary="Test summary",
        )
        self.assertEqual(PolicyDocumentAnalysis.objects.count(), 1)
        doc.delete()
        self.assertEqual(PolicyDocumentAnalysis.objects.count(), 0)


# --- Format for Chat Tests ---


class FormatAnalysisForChatTest(TestCase):
    """Tests for MLPolicyDocHelper.format_analysis_for_chat."""

    def _make_analysis(self):
        doc = PolicyDocument.objects.create(
            document_type="summary_of_benefits",
            filename="test.pdf",
        )
        return PolicyDocumentAnalysis.objects.create(
            policy_document=doc,
            summary="This is a comprehensive policy.",
            exclusions=[
                {
                    "text": "No cosmetic surgery",
                    "page": 5,
                    "explanation": "Cosmetic procedures excluded",
                }
            ],
            inclusions=[
                {
                    "text": "Preventive care covered",
                    "page": 2,
                    "explanation": "Annual checkups",
                }
            ],
            appeal_clauses=[
                {"text": "30-day appeal window", "page": 10, "type": "appeal_deadline"}
            ],
            quotable_sections=[
                {
                    "quote": "All medically necessary care is covered",
                    "page": 3,
                    "relevance": "Broad coverage clause",
                }
            ],
        )

    def test_all_sections_rendered(self):
        analysis = self._make_analysis()
        output = MLPolicyDocHelper.format_analysis_for_chat(analysis)
        self.assertIn("## Summary", output)
        self.assertIn("Key Exclusions", output)
        self.assertIn("Key Coverage", output)
        self.assertIn("Useful for Appeals", output)
        self.assertIn("Quotable Sections", output)

    def test_includes_disclaimer_by_default(self):
        analysis = self._make_analysis()
        output = MLPolicyDocHelper.format_analysis_for_chat(analysis)
        self.assertIn("Important Disclaimer", output)

    def test_can_suppress_disclaimer(self):
        analysis = self._make_analysis()
        output = MLPolicyDocHelper.format_analysis_for_chat(
            analysis, include_disclaimer=False
        )
        self.assertNotIn("Important Disclaimer", output)

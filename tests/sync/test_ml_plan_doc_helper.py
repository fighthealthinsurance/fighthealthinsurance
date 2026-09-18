"""Regression tests for ``MLPlanDocHelper.extract_relevant_text`` decryption.

Before this fix, ``extract_relevant_text`` read ``plan_document_enc`` via
``file_field.path`` and handed the raw *ciphertext* file to the PDF parser, so
extraction always failed, the exception was swallowed, and the feature silently
yielded "".  These tests save a real encrypted PDF to ``plan_document_enc`` and
assert the decrypted text is returned instead.

These live in ``tests/sync/`` rather than ``tests/async/`` on purpose: they are
synchronous ``TransactionTestCase`` methods that drive the async helper via
``asyncio.run()``. ``TransactionTestCase`` truncates tables and cannot run under
the async suite's parallel (``-n auto``) xdist workers, and the sync suite is
where the ``asyncio.run()``-from-a-sync-test pattern belongs.
"""

import asyncio

import pymupdf
import pytest
from django.core.files.base import ContentFile
from django.test import TransactionTestCase

from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper
from fighthealthinsurance.models import Denial, PlanDocuments


def _make_pdf_bytes(text: str) -> bytes:
    """Build a one-page PDF containing *text* and return its raw bytes."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes: bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.mark.django_db
class ExtractRelevantTextDecryptionTests(TransactionTestCase):
    def _make_denial(self) -> Denial:
        return Denial.objects.create(
            hashed_email="hash:plan-doc",
            denial_text="Sample denial for plan document extraction.",
        )

    def test_extract_relevant_text_decrypts_encrypted_pdf(self):
        """An encrypted PDF whose text matches a term yields decrypted text."""
        denial = self._make_denial()
        pdf_bytes = _make_pdf_bytes(
            "This plan covers services subject to medical necessity review."
        )
        doc = PlanDocuments(denial=denial)
        # EncryptedFileField encrypts the bytes on save.
        doc.plan_document_enc.save("plan.pdf", ContentFile(pdf_bytes), save=True)

        result = asyncio.run(
            MLPlanDocHelper.extract_relevant_text(
                denial.denial_id, ["medical necessity"]
            )
        )

        # Prior to the decryption fix this returned "" because the ciphertext
        # was handed to the PDF parser. The decrypted text must now come back
        # (assertIn also proves the result is non-empty).
        self.assertIn("medical necessity", result.lower())

    def test_extract_relevant_text_skips_docs_without_matching_terms(self):
        """A decrypted doc with no matching term contributes no text."""
        denial = self._make_denial()
        pdf_bytes = _make_pdf_bytes("This document is only about dental cleanings.")
        doc = PlanDocuments(denial=denial)
        doc.plan_document_enc.save("plan.pdf", ContentFile(pdf_bytes), save=True)

        result = asyncio.run(
            MLPlanDocHelper.extract_relevant_text(
                denial.denial_id, ["prior authorization"]
            )
        )

        self.assertEqual(result, "")


class WhatItWillAndWillNotParseTest(TransactionTestCase):
    """The parser runs on a shared worker, so what reaches it is bounded."""

    def test_a_document_over_the_ceiling_is_skipped_not_parsed(self):
        from unittest.mock import patch

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("big@example.com"),
            denial_text="Denied.",
        )
        oversized = b"%PDF-1.4\n" + b"x" * (MLPlanDocHelper.MAX_DOCUMENT_BYTES + 1)
        doc = PlanDocuments(denial=denial)
        doc.plan_document_enc.save("huge.pdf", ContentFile(oversized), save=True)

        with patch.object(MLPlanDocHelper, "_extract_pages_with_terms") as extracted:
            result = asyncio.run(
                MLPlanDocHelper.extract_relevant_text(
                    denial.denial_id, ["medical necessity"]
                )
            )

        extracted.assert_not_called()
        self.assertEqual(result, "")

    def test_parsing_is_serialized(self):
        """PyMuPDF is not safe to drive from several threads at once."""
        from fighthealthinsurance.ml import ml_plan_doc_helper

        self.assertIsNotNone(ml_plan_doc_helper._PARSING)
        held = ml_plan_doc_helper._PARSING.acquire(blocking=False)
        self.assertTrue(held, "the lock should be free between parses")
        ml_plan_doc_helper._PARSING.release()


class EveryAcceptedUploadIsReadTest(TransactionTestCase):
    """The upload form takes any file. A format the extractor cannot read is
    a document the patient handed over and the appeal never saw."""

    def _text_from(self, name, body):
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(f"{name}@example.com"),
            denial_text="Denied.",
        )
        doc = PlanDocuments(denial=denial)
        doc.plan_document_enc.save(name, ContentFile(body), save=True)
        return asyncio.run(
            MLPlanDocHelper.extract_relevant_text(
                denial.denial_id, ["medical necessity"]
            )
        )

    def test_a_saved_web_page_is_read(self):
        html = (
            b"<html><head><style>p{color:red}</style></head><body>"
            b"<script>var x=1</script>"
            b"<p>Coverage requires medical necessity review.</p>"
            b"</body></html>"
        )

        text = self._text_from("plan.html", html)

        self.assertIn("medical necessity", text.lower())
        self.assertNotIn("var x", text, "script contents are not document text")
        self.assertNotIn("color:red", text, "style contents are not document text")

    def test_markdown_is_read(self):
        text = self._text_from(
            "plan.md", b"# Plan\n\nServices need **medical necessity** review.\n"
        )

        self.assertIn("medical necessity", text.lower())

    def test_a_format_it_cannot_read_yields_nothing_rather_than_raising(self):
        text = self._text_from("scan.jpeg", b"\xff\xd8\xff\xe0 not text at all")

        self.assertEqual(text, "")

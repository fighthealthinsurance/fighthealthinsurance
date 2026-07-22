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
        # was handed to the PDF parser. The decrypted text must now come back.
        self.assertNotEqual(result, "")
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

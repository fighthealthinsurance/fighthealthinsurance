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
from unittest import mock

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

    def test_an_oversized_document_is_never_read_whole(self):
        """Skipping it after reading it costs the memory anyway.

        Rejecting on the length of what came back still allocates the whole
        file, and decryption allocates a second copy, on a worker other
        patients are generating on. So the limit has to reach the read.
        """
        from fighthealthinsurance.ml.ml_document_extraction import (
            read_and_decrypt_file,
        )

        class WatchedFile:
            """Stands in for the stored file and records how it is read."""

            def __init__(self, body):
                self.body = body
                self.asked_for = []

            def read(self, size=None):
                self.asked_for.append(size)
                return self.body if size is None else self.body[:size]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        watched = WatchedFile(b"y" * 4096)
        field = mock.Mock()
        field.name = "huge.pdf"
        field.open.return_value = watched

        read_and_decrypt_file(field, 1024)

        self.assertTrue(watched.asked_for, "the file was never read")
        self.assertNotIn(
            None,
            watched.asked_for,
            "the whole file was read before its size was judged",
        )

    def test_two_parses_never_overlap(self):
        """PyMuPDF is not safe to drive from several threads at once.

        Asserting the lock object exists proved nothing: removing the `with`
        around the parse left both assertions passing. This runs two parses
        from two threads and records when each is inside, so an unguarded
        parser overlaps and fails.
        """
        import threading
        import time

        from fighthealthinsurance.ml import ml_document_extraction

        inside = []
        overlapped = []
        real = ml_document_extraction.extract_text_from_plaintext_bytes

        def slow(data):
            inside.append(1)
            if len(inside) > 1:
                overlapped.append(1)
            time.sleep(0.05)
            inside.pop()
            return real(data)

        with mock.patch.object(
            ml_document_extraction, "extract_text_from_plaintext_bytes", slow
        ):
            threads = [
                threading.Thread(
                    target=ml_document_extraction.extract_text_from_bytes,
                    args=(b"plan text", "plan.txt"),
                )
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(overlapped, [], "two parses ran at the same time")

    def test_every_caller_of_the_parser_is_covered(self):
        """The lock is at the parser, not at one of its call sites.

        Two helpers parse documents. Guarding only the plan-document one
        leaves the other free to enter the parser at the same time, which is
        the case that crashes the worker.
        """
        import inspect

        from fighthealthinsurance.ml import ml_document_extraction

        source = inspect.getsource(ml_document_extraction.extract_text_from_bytes)
        self.assertIn("with _PARSING:", source)


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
                denial.denial_id,
                ["medical necessity", "necesidad", "autorizaci\u00f3n"],
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

    def test_an_inline_tag_does_not_split_a_phrase(self):
        """A term spanning markup still matches.

        Insurers bold half a phrase all the time, and joining the pieces
        with a newline made "medical <strong>necessity</strong>" stop
        matching a search for "medical necessity".
        """
        html = b"<html><body><p>Requires medical <strong>necessity</strong> review.</p></body></html>"

        text = self._text_from("bolded.html", html)

        self.assertIn("medical necessity", text.lower())

    def test_a_long_page_is_not_dropped_whole(self):
        """One giant section is all or nothing against the caller's budget.

        A policy page that mentions the term once was parsed successfully
        and then discarded entirely for being longer than the budget, so the
        patient's document contributed nothing.
        """
        filler = "Plan information. " * 800
        html = (
            "<html><body><p>Coverage requires medical necessity review.</p>"
            f"<p>{filler}</p></body></html>"
        ).encode()

        text = self._text_from("long.html", html)

        self.assertIn("medical necessity", text.lower())

    def test_undeclared_utf8_is_not_guessed_at(self):
        """Mostly ASCII with one accent loses a single-byte guess."""
        html = ("<p>" + "Plan information. " * 100 + "autorizaci\u00f3n</p>").encode(
            "utf-8"
        )

        text = self._text_from("undeclared.html", html)

        self.assertIn("autorizaci\u00f3n", text)

    def test_a_page_in_another_encoding_keeps_its_words(self):
        """A plan document from a Spanish-language portal is often Latin-1.

        Decoding as UTF-8 first turns the accented letter into a replacement
        character, and then the search term does not match the text it is in.
        """
        html = (
            (
                '<html><head><meta charset="ISO-8859-1"></head><body>'
                "<p>Se requiere autorizacion previa por necesidad medica.</p>"
                "</body></html>"
            )
            .replace("autorizacion", "autorizaci\u00f3n")
            .replace("medica", "m\u00e9dica")
        )

        text = self._text_from("plan.html", html.encode("latin-1"))

        self.assertIn("autorizaci\u00f3n", text)
        self.assertNotIn("\ufffd", text, "a character was lost decoding the file")

    def test_markdown_is_read(self):
        text = self._text_from(
            "plan.md", b"# Plan\n\nServices need **medical necessity** review.\n"
        )

        self.assertIn("medical necessity", text.lower())

    def test_a_format_it_cannot_read_yields_nothing_rather_than_raising(self):
        text = self._text_from("scan.jpeg", b"\xff\xd8\xff\xe0 not text at all")

        self.assertEqual(text, "")

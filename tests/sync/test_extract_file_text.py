import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

from django.test import TestCase

from fighthealthinsurance.utils import extract_file_text


class ExtractFileTextPlainTextTest(TestCase):
    """Tests for extract_file_text with plain text files."""

    def test_reads_text_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            f.flush()
            result = extract_file_text(f.name)
        os.unlink(f.name)
        self.assertEqual(result, "hello world")

    def test_missing_file_returns_empty(self):
        result = extract_file_text("/nonexistent/path/file.txt")
        self.assertEqual(result, "")

    @patch("builtins.open", side_effect=PermissionError("access denied"))
    def test_unreadable_file_returns_empty(self, _mock_open):
        result = extract_file_text("/some/unreadable/file.txt")
        self.assertEqual(result, "")


class ExtractFileTextPDFTest(TestCase):
    """Tests for extract_file_text with PDF files."""

    def test_pymupdf_import_error_returns_empty(self):
        # Temporarily remove pymupdf from sys.modules and block re-import
        saved = sys.modules.pop("pymupdf", None)
        import builtins

        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "pymupdf":
                raise ImportError("no pymupdf")
            return real_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=blocking_import):
                result = extract_file_text("/some/file.pdf")
            self.assertEqual(result, "")
        finally:
            if saved is not None:
                sys.modules["pymupdf"] = saved

    @patch("pymupdf.open")
    def test_pdf_extraction_success(self, mock_open):
        mock_page1 = MagicMock()
        mock_page1.get_text.return_value = "page one"
        mock_page2 = MagicMock()
        mock_page2.get_text.return_value = "page two"
        mock_doc = MagicMock()
        mock_doc.__enter__ = MagicMock(return_value=[mock_page1, mock_page2])
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_doc
        result = extract_file_text("/fake/file.pdf")
        self.assertEqual(result, "page one\npage two")

    @patch("pymupdf.open")
    def test_pdf_runtime_error_returns_empty(self, mock_open):
        mock_open.side_effect = RuntimeError("corrupt PDF")
        result = extract_file_text("/fake/file.pdf")
        self.assertEqual(result, "")

    @patch("pymupdf.open")
    def test_pdf_file_not_found_returns_empty(self, mock_open):
        mock_open.side_effect = FileNotFoundError("no such file")
        result = extract_file_text("/fake/file.pdf")
        self.assertEqual(result, "")

    @patch("pymupdf.open")
    def test_pdf_permission_error_returns_empty(self, mock_open):
        mock_open.side_effect = PermissionError("access denied")
        result = extract_file_text("/fake/file.pdf")
        self.assertEqual(result, "")

    @patch("pymupdf.open")
    def test_pdf_generic_exception_returns_empty(self, mock_open):
        mock_open.side_effect = ValueError("unexpected error")
        result = extract_file_text("/fake/file.pdf")
        self.assertEqual(result, "")

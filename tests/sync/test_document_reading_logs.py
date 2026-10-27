"""Document reading logs say what kind of error happened, and nothing more.

Each reading path is fed a file named after Rebecca Lee Crumpler, or an error
whose message names her. What it logs may say what went wrong, but never her
name.
"""

from contextlib import contextmanager
from unittest import mock

import pytest
from loguru import logger

from fighthealthinsurance import pa_requirement_parsers, pdf_text
from fighthealthinsurance.ml import ml_document_extraction as extraction

NAME = "Rebecca Lee Crumpler"


@contextmanager
def captured_logs():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        yield lines
    finally:
        logger.remove(sink)


class StoredFile:
    """Stands in for an opened EncryptedFileField."""

    def __init__(self, body):
        self.body = body

    def read(self, size=None):
        return self.body if size is None else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stored_field():
    field = mock.Mock()
    field.name = f"{NAME} plan.pdf"
    return field


def a_file_over_the_limit():
    field = stored_field()
    field.open.return_value = StoredFile(b"x" * 64)
    assert extraction.read_and_decrypt_file(field, 16) is None


def a_file_that_cannot_be_opened():
    field = stored_field()
    field.open.side_effect = FileNotFoundError(f"No such file: '{NAME} plan.pdf'")
    assert extraction.read_and_decrypt_file(field) is None


def a_pdf_that_cannot_be_read():
    error = RuntimeError(f"cannot open {NAME} plan.pdf")
    with mock.patch.object(extraction, "read_mupdf_pages", side_effect=error):
        assert extraction.extract_text_from_pdf_bytes(b"%PDF-1.4") == ("", {})


def a_pa_list_that_cannot_be_read():
    error = ValueError(f"cannot open {NAME} plan.pdf")
    pdf = f"%PDF-1.4 {NAME}".encode()
    with mock.patch.object(pdf_text, "read_mupdf_pages", side_effect=error):
        assert pa_requirement_parsers.parse_pdf_pa_list(pdf, "payer.pdf") == []


def a_pdf_that_takes_too_long():
    with mock.patch.object(extraction, "MAX_PDF_SECONDS", 0.01):
        assert extraction.extract_text_from_pdf_bytes(b"%PDF-1.4") == ("", {})


class Undecodable(bytes):
    def decode(self, *args, **kwargs):
        raise UnicodeDecodeError("utf-8", NAME.encode(), 0, 1, f"near {NAME}")


def a_text_file_that_cannot_be_decoded():
    assert extraction.extract_text_from_plaintext_bytes(Undecodable(b"x")) == (
        "",
        {},
    )


def a_type_with_no_parser():
    assert extraction.extract_text_from_bytes(b"x", f"{NAME}.pages") == ("", {})


def a_common_type_with_no_parser():
    assert extraction.extract_text_from_bytes(b"x", f"{NAME} plan.RTF") == ("", {})


#: Each path, and the words its log uses to say what kind of error it was.
PATHS = [
    pytest.param(a_file_over_the_limit, "larger than", id="over the limit"),
    pytest.param(a_file_that_cannot_be_opened, "FileNotFoundError", id="open"),
    pytest.param(a_pdf_that_cannot_be_read, "RuntimeError", id="pdf"),
    pytest.param(a_pdf_that_takes_too_long, "TimeoutError", id="pdf timeout"),
    pytest.param(a_pa_list_that_cannot_be_read, "ValueError", id="pa pdf"),
    pytest.param(a_text_file_that_cannot_be_decoded, "UnicodeDecodeError", id="text"),
    pytest.param(a_type_with_no_parser, "unsupported type", id="unknown type"),
    pytest.param(a_common_type_with_no_parser, ".rtf file", id="common type"),
]


@pytest.mark.parametrize("read, kind", PATHS)
def test_a_reading_log_carries_nothing_from_the_file(read, kind):
    with captured_logs() as lines:
        read()
    assert lines and not [line for line in lines if "crumpler" in line.lower()]


@pytest.mark.parametrize("read, kind", PATHS)
def test_a_reading_log_says_what_kind_of_error_it_was(read, kind):
    with captured_logs() as lines:
        read()
    assert [line for line in lines if kind in line]

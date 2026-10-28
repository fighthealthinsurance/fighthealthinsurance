"""Small PDFs built in memory for tests that read PDFs."""

import io
import os
import re
import textwrap
from typing import Optional, Sequence

import pymupdf
import pypdf


def make_pdf_bytes(
    page_texts: Sequence[str], user_password: Optional[str] = None
) -> bytes:
    """Build a PDF with one page per entry in ``page_texts``.

    With ``user_password`` set (the empty string included), the PDF is AES-256
    encrypted: it opens with that password and has a separate owner password.
    """
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    if user_password is None:
        pdf_bytes: bytes = doc.tobytes()
    else:
        pdf_bytes = doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-password",
            user_pw=user_password,
        )
    doc.close()
    return pdf_bytes


def make_shared_stream_pdf_bytes(page_count: int, text_operations: int) -> bytes:
    """Build a PDF whose ``page_count`` pages all draw one content stream.

    The stream shows the letter "a" ``text_operations`` times, so reading the
    text of every page parses that stream once per page while the file itself
    stays a few kilobytes.
    """
    return _shared_stream_pdf_bytes(page_count, b"(a) Tj\n" * text_operations)


def make_shared_long_string_pdf_bytes(page_count: int, string_length: int) -> bytes:
    """Build a PDF whose ``page_count`` pages all show one long string.

    The pages share one compressed content stream that shows ``string_length``
    letters in a single string, so each page reads as ``string_length``
    characters of text while the file itself stays a few kilobytes.
    """
    return _shared_stream_pdf_bytes(page_count, b"(" + b"a" * string_length + b") Tj\n")


def _shared_stream_pdf_bytes(page_count: int, text_operations: bytes) -> bytes:
    """Build a PDF whose ``page_count`` pages all run ``text_operations``."""
    doc = pymupdf.open()
    first = doc.new_page()
    first.insert_text((72, 72), "a")
    contents_xref = first.get_contents()[0]
    doc.update_stream(
        contents_xref,
        b"BT /helv 11 Tf 72 720 Td\n" + text_operations + b"ET\n",
    )
    _, resources = doc.xref_get_key(first.xref, "Resources")
    for _ in range(page_count - 1):
        page = doc.new_page()
        doc.xref_set_key(page.xref, "Contents", f"{contents_xref} 0 R")
        doc.xref_set_key(page.xref, "Resources", resources)
    pdf_bytes: bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def make_table_pdf_bytes(rows: Sequence[Sequence[str]], text: str = "") -> bytes:
    """Build a one-page PDF with ``rows`` drawn as a ruled table.

    Each cell is a box with its text inside, so PyMuPDF's table finder sees
    the lines. ``text``, if given, is written above the table.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 60), text)
    width, height = 160, 24
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            box = pymupdf.Rect(
                72 + c * width, 100 + r * height, 72 + (c + 1) * width, 100 + (r + 1) * height
            )
            page.draw_rect(box, color=(0, 0, 0), width=0.8)
            page.insert_text((box.x0 + 4, box.y1 - 8), cell, fontsize=9)
    pdf_bytes: bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def pandoc_stand_in(fails_for: Sequence[str] = ()):
    """A stand-in for _try_pandoc_engines that writes the input's text, with
    any HTML tags dropped, to a PDF where pandoc would, so appeal assembly runs
    without pandoc installed. An input whose file name starts with one of
    ``fails_for`` fails the way pandoc does when every engine has failed."""

    async def _engines(command: list[str]) -> None:
        output_path = next(arg[2:] for arg in command if arg.startswith("-o"))
        input_path = output_path[: -len(".pdf")]
        if os.path.basename(input_path).startswith(tuple(fails_for)):
            raise Exception(f"Failed {input_path} with all engines")
        with open(input_path, encoding="utf-8") as f:
            text = re.sub(r"<[^>]+>", " ", f.read())
        lines = textwrap.wrap(text, 80) or [""]
        pages = ["\n".join(lines[i : i + 40]) for i in range(0, len(lines), 40)]
        with open(output_path, "wb") as f:
            f.write(make_pdf_bytes(pages))

    return _engines


def pdf_text(content: bytes) -> str:
    """The text of every page of a PDF, with runs of whitespace made single
    spaces so a phrase reads the same wherever its lines wrapped."""
    reader = pypdf.PdfReader(io.BytesIO(content))
    return " ".join(" ".join(page.extract_text() for page in reader.pages).split())

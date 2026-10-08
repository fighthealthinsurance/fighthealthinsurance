"""Small PDFs built in memory for tests that read PDFs."""

from typing import Optional, Sequence

import pymupdf


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
    doc = pymupdf.open()
    first = doc.new_page()
    first.insert_text((72, 72), "a")
    contents_xref = first.get_contents()[0]
    doc.update_stream(
        contents_xref,
        b"BT /helv 11 Tf 72 720 Td\n" + b"(a) Tj\n" * text_operations + b"ET\n",
    )
    _, resources = doc.xref_get_key(first.xref, "Resources")
    for _ in range(page_count - 1):
        page = doc.new_page()
        doc.xref_set_key(page.xref, "Contents", f"{contents_xref} 0 R")
        doc.xref_set_key(page.xref, "Resources", resources)
    pdf_bytes: bytes = doc.tobytes()
    doc.close()
    return pdf_bytes

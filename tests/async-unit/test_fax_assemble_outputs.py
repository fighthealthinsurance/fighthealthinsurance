"""FlexibleFaxMagic.assemble_outputs splits PDF pages into headed transmissions.

The header page normally comes from pandoc. Here it is rendered with PyMuPDF
instead, so the page counting and splitting run without pandoc installed.
"""

import os
from unittest.mock import patch

import pymupdf
import pytest
from pypdf import PdfReader

from fighthealthinsurance.fax_utils import FlexibleFaxMagic
from tests.pdf_fixtures import make_pdf_bytes


async def _render_header_with_pymupdf(command: list[str]) -> None:
    """Stand in for pandoc: write the input text file out as a one-page PDF."""
    input_path = next(arg for arg in command[1:] if not arg.startswith("-"))
    output_path = next(arg[2:] for arg in command if arg.startswith("-o"))
    with open(input_path) as f:
        text = f.read()
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 540, 720), text)
    doc.save(output_path)
    doc.close()


@pytest.mark.asyncio
async def test_pdf_pages_are_split_into_headed_transmissions(tmp_path):
    letter = tmp_path / "letter.pdf"
    letter.write_bytes(make_pdf_bytes(["Page one", "Page two", "Page three"]))
    fax_magic = FlexibleFaxMagic([], max_pages=2)

    with patch(
        "fighthealthinsurance.fax_utils._try_pandoc_engines",
        _render_header_with_pymupdf,
    ):
        outputs = await fax_magic.assemble_outputs(
            user_header="MyHeader", extra="", input_paths=[str(letter)]
        )

    try:
        first, second = (PdfReader(path) for path in outputs)
        assert len(first.pages) == 3
        assert "MyHeader" in first.pages[0].extract_text()
        assert "Page two" in first.pages[2].extract_text()
        assert len(second.pages) == 2
        assert "Page three" in second.pages[1].extract_text()
    finally:
        for path in outputs:
            os.remove(path)

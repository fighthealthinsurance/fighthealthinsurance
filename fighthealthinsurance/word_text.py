"""
Reading the text of Word (.docx) files in one pass.

:func:`docx_text` streams word/document.xml through an XML pull parser and
keeps only the elements still open, so it reads the body once, in order.
Paragraphs, tabs and line breaks keep their places, so a table reads cell by
cell. It stops at the caps it is given: characters collected, seconds spent
and bytes unpacked. It logs nothing and raises nothing, so nothing from inside
a file reaches the logs or an error report. It has no Django imports, so any
worker can use it as is.
"""

import io
import time
import zipfile

from lxml import etree

#: Read to the end of the body, or to the character cap. The text can run a
#: little past that cap, so the caller cuts it to its own.
WHOLE = "whole"
#: The time or unpacked-size cap was reached, or the body broke off, after
#: some text was read.
PARTIAL = "partial"
#: No text could be read.
UNREADABLE = "unreadable"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BREAKS = {_W + "p": "\n", _W + "tab": "\t", _W + "br": "\n", _W + "cr": "\n"}
_CHUNK_BYTES = 1024 * 1024


def docx_text(
    content: bytes, max_chars: int, max_seconds: float, max_unpacked_bytes: int
) -> tuple[str, str]:
    """The body text of a .docx in one pass, and WHOLE, PARTIAL or UNREADABLE."""
    pieces: list[str] = []
    chars = 0
    unpacked = 0
    status = WHOLE
    deadline = time.monotonic() + max_seconds
    try:
        parser = etree.XMLPullParser(
            events=("end",), resolve_entities=False, no_network=True, load_dtd=False
        )
        with (
            zipfile.ZipFile(io.BytesIO(content)) as archive,
            archive.open("word/document.xml") as body,
        ):
            while chars < max_chars:
                chunk = body.read(_CHUNK_BYTES)
                if not chunk:
                    break
                unpacked += len(chunk)
                if unpacked > max_unpacked_bytes or time.monotonic() > deadline:
                    status = PARTIAL
                    break
                parser.feed(chunk)
                for _, element in parser.read_events():
                    if element.tag == _W + "t":
                        piece = element.text or ""
                    else:
                        piece = _BREAKS.get(element.tag, "")
                    if piece:
                        pieces.append(piece)
                        chars += len(piece)
                    # Drop what has been read, so only the open elements stay.
                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del element.getparent()[0]
    except Exception:
        if not pieces:
            return "", UNREADABLE
        status = PARTIAL
    return "".join(pieces).strip(), status

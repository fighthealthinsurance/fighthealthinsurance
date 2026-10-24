"""
Reading the text of Word (.docx) files in one pass.

:func:`docx_text` streams word/document.xml through an XML pull parser and
keeps only the elements still open, so it reads the body once, in order.
Paragraphs, tabs, line breaks and non-breaking hyphens keep their places,
so a table reads cell by cell. Content saved in more than one form
(mc:AlternateContent, as Word saves a text box) is read once, from its
first mc:Choice. It stops at the caps it is given: characters collected,
seconds spent and bytes unpacked. It logs nothing and raises nothing, so
nothing from inside a file reaches the logs or an error report. It has no
Django imports, so any worker can use it as is.
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
_BREAKS = {
    _W + "p": "\n",
    _W + "tab": "\t",
    _W + "ptab": "\t",
    _W + "br": "\n",
    _W + "cr": "\n",
    _W + "noBreakHyphen": "-",
}
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_ALTERNATE_CONTENT = _MC + "AlternateContent"
_CHOICE = _MC + "Choice"
_FALLBACK = _MC + "Fallback"
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
    # Depth inside an alternative that repeats one already read.
    skipping = 0
    # For each open mc:AlternateContent, whether one of its alternatives was read.
    choice_read: list[bool] = []
    try:
        parser = etree.XMLPullParser(
            events=("start", "end"),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
        )
        with (
            zipfile.ZipFile(io.BytesIO(content)) as archive,
            archive.open("word/document.xml") as body,
        ):
            while chars < max_chars:
                chunk = body.read(_CHUNK_BYTES)
                if not chunk:
                    # Raises if the body ended before its markup closed.
                    parser.close()
                    break
                unpacked += len(chunk)
                if unpacked > max_unpacked_bytes or time.monotonic() > deadline:
                    status = PARTIAL
                    break
                parser.feed(chunk)
                for event, element in parser.read_events():
                    tag = element.tag
                    if event == "start":
                        if skipping:
                            skipping += 1
                        elif tag == _ALTERNATE_CONTENT:
                            choice_read.append(False)
                        elif tag in (_CHOICE, _FALLBACK) and choice_read:
                            if choice_read[-1]:
                                skipping = 1
                            else:
                                choice_read[-1] = True
                        continue
                    if skipping:
                        skipping -= 1
                        piece = ""
                    elif tag == _W + "t":
                        piece = element.text or ""
                    else:
                        if tag == _ALTERNATE_CONTENT:
                            choice_read.pop()
                        piece = _BREAKS.get(tag, "")
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

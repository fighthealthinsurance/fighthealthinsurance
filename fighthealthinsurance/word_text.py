"""
Reading the text of Word (.docx) files in one pass.

:func:`docx_text` streams the main document part (named by the package's
officeDocument relationship, word/document.xml by default) through an XML
pull parser and keeps only the elements still open, so it reads the body
once, in order. Text a tracked change moved away (w:moveFrom) is skipped;
it is read where it moved to.
Paragraphs, tabs, line breaks and non-breaking hyphens keep their places,
so a table reads cell by cell. Content saved in more than one form
(mc:AlternateContent, as Word saves a text box) is read once, from its
first supported mc:Choice, or its fallback. It stops at the caps it is
given: characters collected, seconds spent and bytes unpacked. It logs nothing and raises nothing, so
nothing from inside a file reaches the logs or an error report. It has no
Django imports, so any worker can use it as is.
"""

import io
import posixpath
import time
import zipfile

from lxml import etree

#: Read to the end of the body, or to the character cap.
WHOLE = "whole"
#: The time or unpacked-size cap was reached, or the body broke off, after
#: some text was read.
PARTIAL = "partial"
#: No text could be read.
UNREADABLE = "unreadable"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W_STRICT = "{http://purl.oclc.org/ooxml/wordprocessingml/main}"
_BREAKS = {
    _W + "p": "\n",
    _W + "tab": "\t",
    _W + "ptab": "\t",
    _W + "br": "\n",
    _W + "cr": "\n",
    _W + "noBreakHyphen": "-",
}
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
# Wordprocessing shapes store their text in ordinary w:txbxContent.
_SUPPORTED_CHOICE_URIS = {
    _W[1:-1],
    _W_STRICT[1:-1],
    _MC[1:-1],
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
}
_ALTERNATE_CONTENT = _MC + "AlternateContent"
_CHOICE = _MC + "Choice"
_FALLBACK = _MC + "Fallback"
_MOVE_FROM = _W + "moveFrom"
_CHUNK_BYTES = 1024 * 1024
_DEFAULT_BODY = "word/document.xml"
_RELS_BYTES = 64 * 1024
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_DOCUMENT_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
}


def _main_part(archive: zipfile.ZipFile) -> str:
    """The body's part name from _rels/.rels, or Word's default."""
    try:
        with archive.open("_rels/.rels") as rels:
            raw = rels.read(_RELS_BYTES + 1)
        if len(raw) > _RELS_BYTES:
            return _DEFAULT_BODY
        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False
        )
        root = etree.fromstring(raw, parser)
        if root.tag != _REL + "Relationships":
            return _DEFAULT_BODY
        for rel in root:
            if (
                rel.tag != _REL + "Relationship"
                or rel.get("Type") not in _OFFICE_DOCUMENT_TYPES
            ):
                continue
            if rel.get("TargetMode") == "External":
                continue
            target = posixpath.normpath(str(rel.get("Target", "")).lstrip("/"))
            if target not in ("", ".") and not target.startswith(".."):
                return target
    except Exception:
        pass
    return _DEFAULT_BODY


def _choice_is_supported(choice: etree._Element) -> bool:
    required = str(choice.get("Requires") or "").split()
    return bool(required) and all(
        choice.nsmap.get(prefix) in _SUPPORTED_CHOICE_URIS for prefix in required
    )


def docx_text(
    content: bytes, max_chars: int, max_seconds: float, max_unpacked_bytes: int
) -> tuple[str, str]:
    """The body text of a .docx in one pass, and WHOLE, PARTIAL or UNREADABLE."""
    pieces: list[str] = []
    chars = 0
    unpacked = 0
    status = WHOLE
    deadline = time.monotonic() + max_seconds
    # Depth inside moved-away text, or an alternative that repeats one already read.
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
            archive.open(_main_part(archive)) as body,
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
                    if tag.startswith(_W_STRICT):
                        tag = _W + tag[len(_W_STRICT) :]
                    if event == "start":
                        if skipping:
                            skipping += 1
                        elif tag == _MOVE_FROM:
                            skipping = 1
                        elif tag == _ALTERNATE_CONTENT:
                            choice_read.append(False)
                        elif tag in (_CHOICE, _FALLBACK) and choice_read:
                            if choice_read[-1] or (
                                tag == _CHOICE and not _choice_is_supported(element)
                            ):
                                skipping = 1
                            else:
                                choice_read[-1] = True
                        continue
                    if skipping:
                        skipping -= 1
                        piece = ""
                    elif tag == _W + "tab" and (
                        element.getparent() is None
                        or element.getparent().tag not in (_W + "r", _W_STRICT + "r")
                    ):
                        # w:pPr/w:tabs/w:tab defines a tab stop, not text.
                        piece = ""
                    elif tag == _W + "t":
                        piece = element.text or ""
                    else:
                        if tag == _ALTERNATE_CONTENT:
                            choice_read.pop()
                        piece = _BREAKS.get(tag, "")
                    if piece:
                        piece = piece[: max_chars - chars]
                        pieces.append(piece)
                        chars += len(piece)
                        if chars == max_chars:
                            return "".join(pieces).strip(), WHOLE
                    # Drop what has been read, so only the open elements stay.
                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del element.getparent()[0]
    except Exception:
        if not pieces:
            return "", UNREADABLE
        status = PARTIAL
    return "".join(pieces).strip(), status

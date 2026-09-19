"""
Shared document text extraction utilities.

Handles PDF, DOCX, and plain text extraction with page/section tracking.
Also provides encrypted file decryption for EncryptedFileField.
"""

import re
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
import io
from typing import Any, Dict, Optional

import pymupdf
from loguru import logger

from django_encrypted_filefield.crypt import Cryptographer


def read_and_decrypt_file(
    file_field: Any, max_bytes: Optional[int] = None
) -> Optional[bytes]:
    """
    Read and decrypt bytes from an EncryptedFileField.

    Falls back to raw bytes if decryption fails (file may be unencrypted).

    ``max_bytes`` bounds the READ, so an oversized document costs that much
    memory and no more. A caller that checks the size of what it got back has
    already paid for the whole file, twice over once decryption allocates its
    own copy, and several patients generating at once multiply that on one
    worker.

    It reads one byte past the limit rather than asking the storage how big
    the file is: ``size`` raises NotImplementedError on the storage backend
    this runs on, the same way ``path`` does, so a check that trusted it
    would quietly never fire.
    """
    try:
        with file_field.open() as f:
            raw_bytes: bytes
            if max_bytes is not None:
                raw_bytes = f.read(max_bytes + 1)
                if len(raw_bytes) > max_bytes:
                    logger.warning(
                        f"{file_field.name} is larger than the {max_bytes} "
                        "bytes this reads; skipping it rather than risking "
                        "the worker other patients are generating on"
                    )
                    return None
            else:
                raw_bytes = f.read()
            if not raw_bytes:
                return None
            try:
                return bytes(Cryptographer.decrypted(raw_bytes))
            except Exception:
                logger.debug("Decryption failed, returning raw bytes as fallback")
                return raw_bytes
    except Exception as e:
        logger.warning(f"Error reading encrypted file: {e}")
        return None


def extract_text_from_pdf_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """
    Extract text from PDF bytes with page number tracking.

    Returns (full_text, page_dict) where page_dict maps page numbers to text.
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for page_num, page in enumerate(doc, start=1):
                page_text = page.get_text()
                if page_text.strip():
                    page_dict[page_num] = page_text
                    full_text += f"\n\n[Page {page_num}]\n{page_text}"
    except Exception as e:
        logger.warning(f"Error extracting text from PDF bytes: {e}")

    return full_text, page_dict


def extract_text_from_pdf_path(
    path: str, search_terms: Optional[list[str]] = None
) -> tuple[str, Dict[int, str]]:
    """
    Extract text from a PDF file path with optional search-term filtering.

    If search_terms is provided, only pages containing at least one term are
    included. Returns (full_text, page_dict).
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    try:
        with pymupdf.open(path) as doc:
            for page_num, page in enumerate(doc, start=1):
                page_text = page.get_text()
                if not page_text.strip():
                    continue
                if search_terms:
                    page_lower = page_text.lower()
                    if not any(t.lower() in page_lower for t in search_terms):
                        continue
                page_dict[page_num] = page_text
                full_text += f"\n\n[Page {page_num}]\n{page_text}"
    except Exception as e:
        logger.warning(f"Error reading PDF {path}: {e}")

    return full_text, page_dict


#: What a .docx may weigh once it is unpacked. Generous for a real plan
#: document, which is a few megabytes of text and some scanned pages.
MAX_DOCX_UNPACKED_BYTES = 64 * 1024 * 1024

#: How much is unpacked at a time. Each read is what bounds the allocation,
#: so it has to stay small next to the total.
_UNPACK_CHUNK = 1024 * 1024


def _bounded_docx(data: bytes) -> Optional[bytes]:
    """The same document, rebuilt from bounded reads, or None if it is too big.

    The stored-bytes limit bounds the archive, not what comes out of it. Zip
    turns a few hundred kilobytes into gigabytes, and this is the first path
    that hands a patient's upload to a real document parser rather than
    decoding it as text.

    Measuring the archive and then handing the original to the parser does
    not bound anything: the parser reads each member itself, in a chunk of
    its own choosing, and a member's output is truncated to its declared
    size only after it has been decompressed. A declared size that lies is
    free, so the allocation happens inside the parser either way.

    So the archive is unpacked here, a megabyte at a time, stopping at the
    first chunk that takes the total over the limit, and rebuilt from what
    came out. The parser then reads a copy whose headers were written from
    the bytes they describe. Stored rather than deflated, because the point
    is to avoid work, not to save space; the copy is what the limit above
    bounds.
    """
    import zipfile

    rebuilt = io.BytesIO()
    unpacked = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive, zipfile.ZipFile(
            rebuilt, "w", zipfile.ZIP_STORED
        ) as copy:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                parts: list[bytes] = []
                with archive.open(entry) as member:
                    while True:
                        chunk = member.read(_UNPACK_CHUNK)
                        if not chunk:
                            break
                        unpacked += len(chunk)
                        if unpacked > MAX_DOCX_UNPACKED_BYTES:
                            logger.warning(
                                f"DOCX unpacks to more than the "
                                f"{MAX_DOCX_UNPACKED_BYTES} bytes this will "
                                "hold; skipping it rather than risking the "
                                "worker other patients are generating on"
                            )
                            return None
                        parts.append(chunk)
                copy.writestr(entry.filename, b"".join(parts))
    except Exception as e:
        # Not a readable archive. Let the parser produce the empty result and
        # log it, rather than inventing a second failure mode here.
        logger.debug(f"Could not unpack the DOCX archive: {e}")
        return data
    return rebuilt.getvalue()


def _iter_docx_text(doc) -> Any:
    """Every paragraph in the body, tables included, in document order.

    ``doc.paragraphs`` walks only the top level, so a plan whose coverage
    criteria or appeal deadline sit in a table loses exactly the provisions
    the letter needs. Walking the body element keeps a table's rows where
    they were written instead of appending them somewhere else.

    Tables nest: a layout table holding a real one is ordinary in a plan
    document, and a cell's own ``text`` stops at its immediate paragraphs,
    so the walk recurses through cells rather than reading them flat.
    """
    yield from _iter_docx_block_text(doc.element.body, doc)


def _iter_docx_block_text(parent, doc) -> Any:
    """Paragraph and table text under one element, in document order."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in parent.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc).text
        elif child.tag == qn("w:tbl"):
            for row in Table(child, doc).rows:
                cells = []
                # A merged cell is returned once per column it spans, and
                # each copy holds whatever is nested in it. Walking every
                # copy repeated that content once per span, which with a
                # nested table repeats again a level down.
                seen_cells = set()
                for cell in row.cells:
                    if id(cell._tc) in seen_cells:
                        continue
                    seen_cells.add(id(cell._tc))
                    # cell._tc is the cell's own element. Private, and the
                    # only way to keep a nested table in the order it was
                    # written; cell.text stops at immediate paragraphs.
                    parts = [
                        part.strip()
                        for part in _iter_docx_block_text(cell._tc, doc)
                        if part and part.strip()
                    ]
                    if parts:
                        cells.append(" ".join(parts))
                if cells:
                    yield " | ".join(cells)


def extract_text_from_docx_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """
    Extract text from DOCX bytes.

    Groups paragraphs into ~3000-char pseudo-pages since DOCX has no
    reliable page boundaries.
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    bounded = _bounded_docx(data)
    if bounded is None:
        return full_text, page_dict

    try:
        import docx

        doc = docx.Document(io.BytesIO(bounded))
        current_section = 1
        current_text = ""
        for para_text in _iter_docx_text(doc):
            text = para_text.strip()
            if not text:
                continue
            current_text += text + "\n"
            if len(current_text) > 3000:
                page_dict[current_section] = current_text
                full_text += f"\n\n[Section {current_section}]\n{current_text}"
                current_section += 1
                current_text = ""
        if current_text.strip():
            page_dict[current_section] = current_text
            full_text += f"\n\n[Section {current_section}]\n{current_text}"
    except Exception as e:
        logger.warning(f"Error extracting text from DOCX bytes: {e}")

    return full_text, page_dict


def extract_text_from_plaintext_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """Extract text from plain text file bytes."""
    full_text = ""
    page_dict: Dict[int, str] = {}

    try:
        content = data.decode("utf-8", errors="replace")
        if content.strip():
            page_dict[1] = content
            full_text = f"\n\n[Section 1]\n{content}"
    except Exception as e:
        logger.warning(f"Error reading plaintext bytes: {e}")

    return full_text, page_dict


#: The one thread documents are parsed on in this process.
#:
#: PyMuPDF documents that driving it from several threads can crash the
#: interpreter, and a crash there does not raise: it takes the worker down
#: with every other generation on it. A single-worker pool serializes them.
#:
#: A pool rather than a lock on the shared executor, because a lock there
#: pins one of its threads for the whole parse while it waits. The default
#: executor is what every other asyncio.to_thread call in the process uses,
#: so a few large documents could occupy all of it and stall work that has
#: nothing to do with documents. Waiting on this pool costs a coroutine, not
#: a thread.
_PARSER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fhi-doc-parse")

#: Still held for a caller that reaches the parser synchronously, which the
#: pool does not serialize on its own.
_PARSING = threading.Lock()


#: Extensions the dispatcher below parses. A caller deciding whether to fall
#: back to reading the bytes as text needs to know which failures mean "no
#: parser for this" and which mean "the parser tried and got nothing".
PARSED_EXTENSIONS = (".pdf", ".docx", ".txt")


class _Payload:
    """A decrypted document, held so it can be let go.

    What is handed to the executor is somebody's plan document in the
    clear. Cancelling the future stops the parse but does not take the work
    item off the queue, and that item holds its arguments until a worker
    dequeues it, which is behind however long the parse in front of it
    takes. Passing the bytes inside this instead means a request that gives
    up can empty it, and what stays on the queue is an empty holder.

    What this does not do, because a thread cannot be cancelled: a parse
    already running keeps its copy until it finishes. That is bounded by the
    document, which is bounded by the read, and it is why the parse is on
    its own thread rather than somewhere a stuck one would be inherited.
    """

    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data

    def take(self) -> bytes:
        """The bytes, and the holder lets go of them."""
        data, self.data = self.data, b""
        return data

    def drop(self) -> None:
        self.data = b""


def _parse_payload(payload: "_Payload", filename: str) -> tuple[str, Dict[int, str]]:
    data = payload.take()
    if not data:
        # Nobody is waiting for this any more.
        return "", {}
    return extract_text_from_bytes(data, filename)


async def aextract_text_from_bytes(
    data: bytes, filename: str
) -> tuple[str, Dict[int, str]]:
    """Parse a document on the parser's own thread.

    The way an async caller should reach the parser: it holds no thread from
    the shared executor while the parse runs.
    """
    loop = asyncio.get_running_loop()
    payload = _Payload(data)
    try:
        return await loop.run_in_executor(_PARSER, _parse_payload, payload, filename)
    except asyncio.CancelledError:
        payload.drop()
        raise


def extract_text_from_bytes(data: bytes, filename: str) -> tuple[str, Dict[int, str]]:
    """Dispatch text extraction by filename extension.

    Serialized: see _PARSER and _PARSING. An extension this does not know
    yields nothing, which callers are entitled to rely on: the policy
    document path treats an empty result as "this file gave us nothing".
    A caller that would rather guess can read the bytes as text itself.
    """
    lower_name = filename.lower()
    with _PARSING:
        if lower_name.endswith(".pdf"):
            return extract_text_from_pdf_bytes(data)
        elif lower_name.endswith(".docx"):
            return extract_text_from_docx_bytes(data)
        elif lower_name.endswith(".txt"):
            return extract_text_from_plaintext_bytes(data)
    logger.warning(f"Unsupported file type for text extraction: {filename}")
    return "", {}

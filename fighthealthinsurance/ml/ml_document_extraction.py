"""
Shared document text extraction utilities.

Handles PDF, DOCX, and plain text extraction with page/section tracking.
Also provides encrypted file decryption for EncryptedFileField.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
from typing import Any, Dict, Optional

from loguru import logger

from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance import word_text
from fighthealthinsurance.pdf_text import read_mupdf_pages


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
                        f"Skipped a stored document larger than the {max_bytes} "
                        "bytes this reads"
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
        logger.warning(f"Could not read a stored document: {type(e).__name__}")
        return None


#: The most pages read from one PDF.
MAX_PDF_PAGES = 2000

#: The most text kept from one PDF, about a thousand pages, like a .docx.
MAX_PDF_CHARS = 3_000_000

#: How long reading one PDF may take, in a child process that is then stopped.
MAX_PDF_SECONDS = 30


def extract_text_from_pdf_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """
    Extract text from PDF bytes with page number tracking.

    Returns (full_text, page_dict) where page_dict maps page numbers to text.
    Read in a child process under the caps above (see pdf_text).
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    try:
        pages = read_mupdf_pages(data, MAX_PDF_PAGES, MAX_PDF_CHARS, MAX_PDF_SECONDS)
    except Exception as e:
        logger.warning(f"Could not read text from a PDF: {type(e).__name__}")
        return full_text, page_dict

    for page_num, page in enumerate(pages, start=1):
        page_text = page["text"]
        if page_text.strip():
            page_dict[page_num] = page_text
            full_text += f"\n\n[Page {page_num}]\n{page_text}"

    return full_text, page_dict


#: What a .docx may weigh once it is unpacked. Generous for a real plan
#: document, which is a few megabytes of text and some scanned pages.
MAX_DOCX_UNPACKED_BYTES = 64 * 1024 * 1024

#: The most text kept from one .docx, about a thousand pages.
MAX_DOCX_CHARS = 3_000_000

#: How long reading one .docx may take.
MAX_DOCX_SECONDS = 30


def extract_text_from_docx_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """
    Extract text from DOCX bytes.

    The body is read in one pass (see word_text), paragraphs and table cells
    in the order they were written, within the caps above. Paragraphs are
    grouped into ~3000-char pseudo-pages since DOCX has no reliable page
    boundaries.
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    text, status = word_text.docx_text(
        data, MAX_DOCX_CHARS, MAX_DOCX_SECONDS, MAX_DOCX_UNPACKED_BYTES
    )
    if status == word_text.UNREADABLE:
        logger.warning("Could not read text from a DOCX")
        return full_text, page_dict
    if status == word_text.PARTIAL:
        logger.warning("Read only part of a DOCX")

    current_section = 1
    current_text = ""
    for line in text[:MAX_DOCX_CHARS].split("\n"):
        line = line.strip()
        if not line:
            continue
        current_text += line + "\n"
        if len(current_text) > 3000:
            page_dict[current_section] = current_text
            full_text += f"\n\n[Section {current_section}]\n{current_text}"
            current_section += 1
            current_text = ""
    if current_text.strip():
        page_dict[current_section] = current_text
        full_text += f"\n\n[Section {current_section}]\n{current_text}"

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
        logger.warning(f"Could not read a text file: {type(e).__name__}")

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

# Unparsed extensions a log may name; any other is logged as unsupported.
_NAMED_UNPARSED_EXTENSIONS = (
    ".doc",
    ".rtf",
    ".odt",
    ".text",
    ".md",
    ".html",
    ".htm",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
)


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
    extension = os.path.splitext(lower_name)[1]
    if extension in _NAMED_UNPARSED_EXTENSIONS:
        logger.warning(f"Text extraction skipped: {extension} file")
    else:
        logger.warning("Text extraction skipped: unsupported type")
    return "", {}

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


#: What a .docx may weigh once it is unpacked.
#:
#: The stored-bytes limit bounds the archive, not what comes out of it: zip
#: is free to turn a few hundred kilobytes into gigabytes, and this is the
#: first path that hands a patient's upload to a real document parser rather
#: than decoding it as text. Deflate cannot fabricate more than the member
#: headers declare -- the reader stops at the declared size -- so adding the
#: declared sizes up before unpacking is a true bound. Generous: a real plan
#: document runs to a few megabytes of text and some scanned pages.
MAX_DOCX_UNPACKED_BYTES = 200 * 1024 * 1024


def _docx_unpacks_too_large(data: bytes) -> bool:
    """Would this archive's declared contents exceed what we will unpack?"""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            declared = sum(entry.file_size for entry in archive.infolist())
    except Exception as e:
        # Not a readable archive. Let the parser produce the empty result
        # and log it, rather than inventing a second failure mode here.
        logger.debug(f"Could not read the DOCX archive to size it: {e}")
        return False
    if declared > MAX_DOCX_UNPACKED_BYTES:
        logger.warning(
            f"DOCX declares {declared} bytes unpacked, over the "
            f"{MAX_DOCX_UNPACKED_BYTES} this will unpack; skipping it rather "
            "than risking the worker other patients are generating on"
        )
        return True
    return False


def _iter_docx_text(doc) -> Any:
    """Every paragraph in the body, tables included, in document order.

    ``doc.paragraphs`` walks only the top level, so a plan whose coverage
    criteria or appeal deadline sit in a table loses exactly the provisions
    the letter needs. Walking the body element keeps a table's rows where
    they were written instead of appending them somewhere else.
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc).text
        elif child.tag == qn("w:tbl"):
            for row in Table(child, doc).rows:
                cells = [cell.text.strip() for cell in row.cells]
                yield " | ".join(cell for cell in cells if cell)


def extract_text_from_docx_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """
    Extract text from DOCX bytes.

    Groups paragraphs into ~3000-char pseudo-pages since DOCX has no
    reliable page boundaries.
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    if _docx_unpacks_too_large(data):
        return full_text, page_dict

    try:
        import docx

        doc = docx.Document(io.BytesIO(data))
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

#: One document queued at a time, for the whole process.
#:
#: The bytes handed to the executor are a decrypted patient document, and a
#: work item sitting in the queue keeps them alive: cancelling the future
#: does not take the item out, so a request that times out waiting can leave
#: somebody's plan document in memory behind a parse that is still running.
#: Waiting here instead keeps the bytes in the request that owns them, where
#: cancelling the task drops them. The parser is one thread, so nothing is
#: lost by queueing in the coroutine rather than in the executor.
_SUBMIT = asyncio.Semaphore(1)


async def aextract_text_from_bytes(
    data: bytes, filename: str
) -> tuple[str, Dict[int, str]]:
    """Parse a document on the parser's own thread.

    The way an async caller should reach the parser: it holds no thread from
    the shared executor while the parse runs.
    """
    loop = asyncio.get_running_loop()
    async with _SUBMIT:
        return await loop.run_in_executor(
            _PARSER, extract_text_from_bytes, data, filename
        )


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

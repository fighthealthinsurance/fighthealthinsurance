"""
Shared document text extraction utilities.

Handles PDF, DOCX, and plain text extraction with page/section tracking.
Also provides encrypted file decryption for EncryptedFileField.
"""

import re
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


def extract_text_from_docx_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """
    Extract text from DOCX bytes.

    Groups paragraphs into ~3000-char pseudo-pages since DOCX has no
    reliable page boundaries.
    """
    full_text = ""
    page_dict: Dict[int, str] = {}

    try:
        import docx

        doc = docx.Document(io.BytesIO(data))
        current_section = 1
        current_text = ""
        for para in doc.paragraphs:
            text = para.text.strip()
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


def extract_text_from_html_bytes(data: bytes) -> tuple[str, Dict[int, str]]:
    """Extract the readable text from an HTML document's bytes.

    Plan documents arrive as whatever the insurer's portal hands the person,
    and a saved web page is common. Without this the upload is accepted and
    then silently contributes nothing to the appeal.
    """
    try:
        from bs4 import BeautifulSoup

        # The bytes, so a document that declares its own encoding is read in
        # it: a plan document saved from an insurer's portal is often
        # Latin-1, and decoding as UTF-8 first turns the accent in
        # "autorizacion" into a replacement character that then matches no
        # search term.
        #
        # But an undeclared document is guessed at, and on mostly-ASCII text
        # with one accented word the guess loses to a single-byte encoding
        # and corrupts exactly that word. So valid UTF-8 with no declaration
        # is decoded as UTF-8 rather than guessed.
        markup: Any = data
        if not _declares_an_encoding(data):
            try:
                markup = data.decode("utf-8")
            except UnicodeDecodeError:
                markup = data

        soup = BeautifulSoup(markup, "html.parser")
        # get_text already leaves script and style contents out, so this is
        # belt and braces: it also drops them from the tree, which keeps
        # them out of anything that later walks it.
        for tag in soup(["script", "style"]):
            tag.decompose()
        # A space, not a newline: inline markup splits a phrase, and
        # "medical <strong>necessity</strong>" has to still read as "medical
        # necessity" or a search for it finds nothing.
        content = re.sub(r"[ \t]*\n[ \t]*", "\n", soup.get_text(" ", strip=True))
    except Exception as e:
        logger.warning(f"Error reading HTML bytes: {e}")
        return "", {}
    if not content.strip():
        return "", {}
    return _as_sections(content)


def _declares_an_encoding(data: bytes) -> bool:
    """Whether the document says what encoding it is in.

    Only the head is examined, which is where a declaration is valid and
    where a parser looks for one.
    """
    head = data[:2048].lower()
    return b"charset" in head or data[:3] == b"\xef\xbb\xbf"


#: How much text goes in one section. A section is the unit a caller keeps or
#: discards, so one giant section is all or nothing: a long policy page that
#: mentions the search term once was being parsed successfully and then
#: dropped whole for exceeding the caller's budget.
SECTION_CHARS = 2000


def _as_sections(content: str) -> tuple[str, Dict[int, str]]:
    """Cut flat text into sections a caller can take some of.

    Split on blank lines first, so a section is a run of related lines, and
    only fall back to a hard cut for a single run that is longer than the
    limit on its own.
    """
    sections: Dict[int, str] = {}
    buffer = ""
    for paragraph in re.split(r"\n\s*\n", content):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > SECTION_CHARS:
            sections[len(sections) + 1] = paragraph[:SECTION_CHARS]
            paragraph = paragraph[SECTION_CHARS:]
        if len(buffer) + len(paragraph) + 1 > SECTION_CHARS and buffer:
            sections[len(sections) + 1] = buffer
            buffer = paragraph
        else:
            buffer = f"{buffer}\n{paragraph}".strip()
    if buffer:
        sections[len(sections) + 1] = buffer
    full = "".join(
        f"\n\n[Section {number}]\n{text}" for number, text in sorted(sections.items())
    )
    return full, sections


#: What ``extract_text_from_bytes`` knows how to read. A plan document in any
#: other format is accepted at upload and then contributes nothing, so a gap
#: here is a patient whose document was taken and not used.
TEXTUAL_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm")


#: Serializes every document parse in this process. PyMuPDF documents that
#: driving it from several threads can crash the interpreter, and these
#: parsers run on executor threads, so two patients generating at once could
#: put two parses in flight. A crash there does not raise: it takes the
#: worker down with every other generation on it. The lock lives here rather
#: than at a call site because there is more than one caller, and the one
#: that is not covered is the one that crashes the worker.
_PARSING = threading.Lock()


def extract_text_from_bytes(data: bytes, filename: str) -> tuple[str, Dict[int, str]]:
    """Dispatch text extraction by filename extension.

    Serialized: see _PARSING. Holding a lock across a slow parse makes other
    patients wait, which is the trade against a crash that loses all of them.
    """
    lower_name = filename.lower()
    with _PARSING:
        if lower_name.endswith(".pdf"):
            return extract_text_from_pdf_bytes(data)
        elif lower_name.endswith(".docx"):
            return extract_text_from_docx_bytes(data)
        elif lower_name.endswith((".html", ".htm")):
            return extract_text_from_html_bytes(data)
        elif lower_name.endswith((".txt", ".md", ".markdown")):
            # Markdown is read as what it is: text a person can read, with
            # its punctuation left in place.
            return extract_text_from_plaintext_bytes(data)
    logger.warning(f"Unsupported file type for text extraction: {filename}")
    return "", {}

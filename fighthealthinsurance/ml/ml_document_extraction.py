"""
Shared document text extraction utilities.

Handles PDF, DOCX, and plain text extraction with page/section tracking.
Also provides encrypted file decryption for EncryptedFileField.
"""

import io
from typing import Any, Dict, Optional

import pymupdf
from loguru import logger

from django_encrypted_filefield.crypt import Cryptographer


def read_and_decrypt_file(file_field: Any) -> Optional[bytes]:
    """
    Read and decrypt bytes from an EncryptedFileField.

    Falls back to raw bytes if decryption fails (file may be unencrypted).
    """
    try:
        with file_field.open() as f:
            raw_bytes: bytes = f.read()
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

        # The bytes, not a string: a plan document saved from an insurer's
        # portal can be Latin-1, and decoding it as UTF-8 first turns
        # "autorizacion" with its accent into a replacement character, which
        # then matches no search term. BeautifulSoup reads the document's own
        # declared encoding.
        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        content = soup.get_text("\n", strip=True)
    except Exception as e:
        logger.warning(f"Error reading HTML bytes: {e}")
        return "", {}
    if not content.strip():
        return "", {}
    return f"\n\n[Section 1]\n{content}", {1: content}


#: What ``extract_text_from_bytes`` knows how to read. A plan document in any
#: other format is accepted at upload and then contributes nothing, so a gap
#: here is a patient whose document was taken and not used.
TEXTUAL_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm")


def extract_text_from_bytes(data: bytes, filename: str) -> tuple[str, Dict[int, str]]:
    """Dispatch text extraction by filename extension."""
    lower_name = filename.lower()
    if lower_name.endswith(".pdf"):
        return extract_text_from_pdf_bytes(data)
    elif lower_name.endswith(".docx"):
        return extract_text_from_docx_bytes(data)
    elif lower_name.endswith((".html", ".htm")):
        return extract_text_from_html_bytes(data)
    elif lower_name.endswith((".txt", ".md", ".markdown")):
        # Markdown is read as what it is: text a person can read, with its
        # punctuation left in place.
        return extract_text_from_plaintext_bytes(data)
    else:
        logger.warning(f"Unsupported file type for text extraction: {filename}")
        return "", {}

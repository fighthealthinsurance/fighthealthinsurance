"""The limits every uploaded or stored file is held to, in one place.

Uploads reach the server from several pages (the appeal flow's plan
documents, Explain My Policy, the research share, follow-up feedback and
server-side OCR), and the files are read back by several more. Each used to
carry its own number, or none. These are the numbers, and the checks the
forms run before a file is read, so the pages and the readers agree.

Only Django is imported here, so both ``forms`` and ``chat_forms`` can use it
without importing each other.
"""

from typing import Iterable, Optional, Sequence

from django import forms
from django.core.files.uploadedfile import UploadedFile

#: The largest file any page takes, as the pages say: "up to 20MB".
MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024
#: The most files one upload takes.
MAX_UPLOAD_FILES = 10
#: All the files in one upload, with room under the proxy's 80MB body limit
#: (conf/nginx.default and the k8s ingress annotations).
MAX_UPLOAD_TOTAL_BYTES = 60 * 1024 * 1024

#: Policy and plan documents: the formats the readers parse.
SUPPORTED_POLICY_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_POLICY_DOCUMENT_BYTES = MAX_UPLOAD_FILE_BYTES

#: The smallest policy or plan document taken: below a short rate sheet
#: saved as text, above a line of test text, which no reader can use.
MIN_POLICY_DOCUMENT_BYTES = 256
TOO_SMALL_DOCUMENT_MESSAGE = (
    "This file is too small to be a policy or plan document. Please upload "
    "the full document as a PDF, Word (.docx) or text file."
)

#: Follow-up documents and appeal attachments: documents and photos of
#: letters. Nothing on the site runs or shows these inline, but a type
#: outside the list (a web page, a script, a program) is refused all the same.
SUPPORTED_ATTACHMENT_EXTENSIONS = SUPPORTED_POLICY_EXTENSIONS | {
    ".doc",
    ".rtf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".webp",
}

#: The longest file name kept, extension included.
MAX_FILENAME_CHARS = 200

# Magic bytes for file type validation
_PDF_MAGIC = b"%PDF"
_DOCX_MAGIC = b"PK"  # ZIP-based format


def stored_size_limit(plaintext_bytes: int) -> int:
    """The most stored bytes a file of at most ``plaintext_bytes`` takes.

    EncryptedFileField stores a Fernet token: base64 of the ciphertext, so
    about a third bigger than the file, plus a little framing. A reader that
    bounds what it reads from storage must allow for that growth, or a file
    the upload accepted is silently skipped (a 20MB upload is stored as
    26.7MB).
    """
    return plaintext_bytes * 4 // 3 + 1024 * 1024


#: The most a reader takes from storage for one policy or plan document: a
#: MAX_POLICY_DOCUMENT_BYTES upload once encrypted, with headroom.
MAX_STORED_POLICY_DOCUMENT_BYTES = 32 * 1024 * 1024
assert MAX_STORED_POLICY_DOCUMENT_BYTES >= stored_size_limit(MAX_POLICY_DOCUMENT_BYTES)


def safe_filename(name: str) -> str:
    """``name`` as kept and shown: no folders, only letters, digits, spaces,
    dots, hyphens and underscores, and at most MAX_FILENAME_CHARS, with its
    extension (lowercased) kept last."""
    import os
    import re

    base = os.path.basename(str(name or "").replace("\\", "/"))
    cleaned = re.sub(r"[^\w .\-]", "_", base).strip(" .") or "file"
    stem, ext = os.path.splitext(cleaned)
    ext = ext.lower()[:10]
    return f"{stem[: max(1, MAX_FILENAME_CHARS - len(ext))]}{ext}"


def megabytes(byte_count: int) -> int:
    return byte_count // (1024 * 1024)


def policy_document_extension(name: str) -> str:
    """The file's extension, lowercased with its dot, or "" when it has none."""
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate_upload_size(
    file: UploadedFile, max_bytes: int = MAX_UPLOAD_FILE_BYTES
) -> None:
    """Raise ValidationError when ``file`` is over ``max_bytes``.

    Django measured the file as it arrived, so ``size`` can be trusted, and
    nothing is read here.
    """
    if file.size is not None and file.size > max_bytes:
        raise forms.ValidationError(
            f"File size must be less than {megabytes(max_bytes)}MB. "
            "Please upload a smaller file."
        )


def validate_policy_document(
    file: UploadedFile, min_bytes: int = MIN_POLICY_DOCUMENT_BYTES
) -> None:
    """Raise ValidationError unless ``file`` is a policy document we can read:
    a supported extension, at most MAX_POLICY_DOCUMENT_BYTES, for PDF and
    DOCX the right first bytes, and at least ``min_bytes``."""
    validate_upload_size(file, MAX_POLICY_DOCUMENT_BYTES)
    ext = policy_document_extension(file.name or "")
    if not ext:
        raise forms.ValidationError(
            f"File must have an extension. Please upload a {', '.join(sorted(SUPPORTED_POLICY_EXTENSIONS))} file."
        )
    if ext not in SUPPORTED_POLICY_EXTENSIONS:
        raise forms.ValidationError(
            f"Unsupported file type. Please upload one of: {', '.join(sorted(SUPPORTED_POLICY_EXTENSIONS))}"
        )
    # Magic byte check for PDF and DOCX (stronger than MIME type)
    header = file.read(8)
    file.seek(0)
    if ext == ".pdf" and not header.startswith(_PDF_MAGIC):
        raise forms.ValidationError("The file does not appear to be a valid PDF.")
    if ext == ".docx" and not header.startswith(_DOCX_MAGIC):
        raise forms.ValidationError(
            "The file does not appear to be a valid DOCX document."
        )
    # Last, so a file that is the wrong kind hears why first.
    if file.size is not None and file.size < min_bytes:
        raise forms.ValidationError(TOO_SMALL_DOCUMENT_MESSAGE)


# First bytes of the image formats a person is likely to photograph a letter in.
_IMAGE_MAGIC = {
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
}


def validate_attachment(
    file: UploadedFile, max_bytes: int = MAX_UPLOAD_FILE_BYTES
) -> None:
    """Raise ValidationError unless ``file`` is a document or photo we keep:
    at most ``max_bytes``, a SUPPORTED_ATTACHMENT_EXTENSIONS type, and for
    PDF, Word and the common image types the right first bytes."""
    validate_upload_size(file, max_bytes)
    ext = policy_document_extension(file.name or "")
    if ext not in SUPPORTED_ATTACHMENT_EXTENSIONS:
        raise forms.ValidationError(
            "Unsupported file type. Please upload a document (PDF, Word or "
            "text) or a photo (PNG, JPEG, GIF, TIFF, HEIC or WebP)."
        )
    header = file.read(8)
    file.seek(0)
    expected = {".pdf": (_PDF_MAGIC,), ".docx": (_DOCX_MAGIC,), **_IMAGE_MAGIC}
    if ext in expected and not header.startswith(expected[ext]):
        raise forms.ValidationError(
            f"The file does not appear to be a valid {ext[1:].upper()} file."
        )


def too_many_files_message(max_files: int = MAX_UPLOAD_FILES) -> str:
    return f"You can upload up to {max_files} files at a time."


def too_much_in_all_message(max_total_bytes: int = MAX_UPLOAD_TOTAL_BYTES) -> str:
    return (
        f"These files add up to more than {megabytes(max_total_bytes)}MB. "
        "Please upload fewer of them at a time."
    )


def validate_uploads(
    files: Sequence[UploadedFile],
    *,
    each: Optional[Iterable] = None,
    max_files: int = MAX_UPLOAD_FILES,
    max_total_bytes: int = MAX_UPLOAD_TOTAL_BYTES,
) -> None:
    """The checks a multi-file field runs on the files it was given.

    Raise ValidationError when there are more than ``max_files``, when they
    add up to more than ``max_total_bytes``, or when any file fails one of
    the ``each`` validators (every failing file is named in one error).
    """
    if len(files) > max_files:
        raise forms.ValidationError(too_many_files_message(max_files))
    if sum(f.size or 0 for f in files) > max_total_bytes:
        raise forms.ValidationError(too_much_in_all_message(max_total_bytes))
    problems: list[str] = []
    for file in files:
        for validator in each or ():
            try:
                validator(file)
            except forms.ValidationError as error:
                problems.extend(f"{file.name}: {message}" for message in error.messages)
                break
    if problems:
        raise forms.ValidationError(problems)


# ---- Server-side OCR --------------------------------------------------------

#: The most images one server-side OCR request takes: a denial, a page an image.
MAX_OCR_FILES = 30
#: Each image, as the intake pages say: up to 20MB.
MAX_OCR_FILE_BYTES = MAX_UPLOAD_FILE_BYTES
#: The most pixels an image may have before it is decoded. Scans of letter
#: pages at 600 dpi are about 30 million; this leaves room for a large photo
#: and refuses an image that would take hundreds of megabytes to decode.
MAX_OCR_IMAGE_PIXELS = 50_000_000
#: How long tesseract may spend on one image.
OCR_SECONDS_PER_IMAGE = 60

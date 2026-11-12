"""Reading shared plan documents, away from the request that stored them.

Each shared file is read once. Its text is pulled out with the bounded
readers (a PDF in a child process, a Word file in one pass, a text file
decoded), personal details are taken out as best we can
(plan_document_scrub), and the cleaned text is stored on the row with where
its reading ended: ready for review, or unreadable when no real text could
be read. The raw text is never stored, and nothing read from a file is
logged or raised.

A PDF page with no text, or with text that reads as garbage
(text_quality.is_garbled: a font with its own encoding gives dots or shifted
letters), is read again with OCR, up to OCR_MAX_PAGES pages and OCR_SECONDS
in all. A page still garbled after that is left out, and the text says how
many pages were. Text that is garbled as a whole is never stored as ready.

Only shared files are read. A shared link has no file, and nothing here,
or anywhere else on the server, fetches it: staff open links by hand.

Rows are looked up by id when the work runs, so a row that remove-my-data
deleted first is simply not there, and one it emptied of its file and text
is marked removed, not waiting. The store only updates a row still waiting,
so a row deleted or emptied during a reading stays that way. The
SharedPlanDocumentsWorkflow activities wrap these
(activities/shared_plan_documents.py).
"""

import asyncio
import datetime
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from channels.db import database_sync_to_async
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.text_quality import is_clearly_text, is_garbled
from fighthealthinsurance.upload_limits import (
    MAX_OCR_IMAGE_PIXELS,
    MAX_STORED_POLICY_DOCUMENT_BYTES,
    OCR_SECONDS_PER_IMAGE,
)

#: What a reading reports for a row that is no longer there.
GONE = "gone"

# The staff text view's caps, with fewer characters, since this text is kept.
MAX_PAGES = 1500
MAX_CHARS = 2_000_000
READ_SECONDS = 60
# The encrypted file is about a third bigger than the 20MB upload limit.
MAX_STORED_BYTES = MAX_STORED_POLICY_DOCUMENT_BYTES
# More rows than one share can hold (ten files on the share page).
MAX_ROWS_PER_BATCH = 50
# OCR for pages with no text or garbled text: the first OCR_MAX_PAGES such
# pages, in OCR_SECONDS in all, each drawn at OCR_DPI within the server OCR's
# pixel cap and time per image. Pages past either bound are left out, and the
# text says how many.
OCR_MAX_PAGES = 40
OCR_SECONDS = 120
OCR_DPI = 200

# Threads of its own for reading files, so these never wait behind, or hold
# up, the PDF reads appeal generation makes in the same worker process.
_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fhi-plan-docs-read")
# OCR's own, so a long scan never makes another file's text read wait.
_OCR_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fhi-plan-docs-ocr")


class FileNotRead(Exception):
    """The stored file could not be read back. Retried; after the last
    attempt the workflow marks the row unreadable."""


def _shared_rows():
    # Shared files only: a shared link is never read, so never fetched.
    return PolicyDocument.objects.filter(source__in=PolicyDocument.SHARED_FILE_SOURCES)


async def abatch_document_ids(share_batch: str) -> list[str]:
    """The ids of the batch's rows still waiting to be read, oldest first."""
    rows = (
        _shared_rows()
        .filter(
            share_batch=share_batch,
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
        )
        .order_by("shared_at", "id")
        .values_list("pk", flat=True)[:MAX_ROWS_PER_BATCH]
    )
    return [str(pk) async for pk in rows]


async def awaiting_batches(received_minutes: int, limit: int) -> list[str]:
    """Up to ``limit`` share batches, oldest first, holding a row shared
    more than ``received_minutes`` ago that is still waiting to be read."""
    cutoff = timezone.now() - datetime.timedelta(minutes=received_minutes)
    rows = (
        _shared_rows()
        .filter(
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            shared_at__lte=cutoff,
            share_batch__isnull=False,
        )
        .order_by("shared_at", "id")
        .values_list("share_batch", flat=True)[: limit * MAX_ROWS_PER_BATCH]
    )
    batches: list[str] = []
    async for batch in rows:
        if str(batch) not in batches:
            batches.append(str(batch))
            if len(batches) >= limit:
                break
    return batches


async def _status_now(document_id: str) -> str:
    status = await (
        _shared_rows()
        .filter(pk=document_id)
        .values_list("processing_status", flat=True)
        .afirst()
    )
    return GONE if status is None else status


def _read_stored(doc: PolicyDocument) -> Optional[bytes]:
    # File work on the storage backend, no ORM.
    from fighthealthinsurance.ml.ml_document_extraction import read_and_decrypt_file

    if not doc.document_enc:
        return None
    return read_and_decrypt_file(doc.document_enc, max_bytes=MAX_STORED_BYTES)


def ocr_available() -> bool:
    """Whether tesseract is on this machine, for the worker to say so."""
    from fighthealthinsurance.pdf_text import TESSERACT

    return shutil.which(TESSERACT[0]) is not None


async def _aocr(
    content: bytes, pages: list[int]
) -> tuple[dict[int, Optional[str]], Optional[str]]:
    """(page -> text read with OCR, or None where OCR failed on the page; why
    pages were left unread, or None).
    Without tesseract, or for a PDF it can't open, no page is read, and the
    reason says so; an OCR that timed out or could not run raises."""
    from fighthealthinsurance.pdf_text import TESSERACT_MISSING, aocr_pdf_pages

    try:
        result = await aocr_pdf_pages(
            content,
            pages[:OCR_MAX_PAGES],
            MAX_CHARS,
            OCR_DPI,
            MAX_OCR_IMAGE_PIXELS,
            OCR_SECONDS_PER_IMAGE,
            OCR_SECONDS,
            executor=_OCR_READERS,
        )
    except ValueError as e:
        # Our own message, or the class alone: MuPDF's can quote the file.
        if str(e) == TESSERACT_MISSING:
            logger.error(
                "OCR could not run on a shared plan document: "
                "tesseract is not installed"
            )
            return {}, "OCR could not run"
        logger.warning(f"OCR could not open a shared plan document: {type(e).__name__}")
        return {}, "OCR could not open the file"
    read = {int(number): text for number, text in result["texts"]}
    if len(pages) > OCR_MAX_PAGES or result["stopped_at"] is not None:
        return read, (
            f"reading scanned pages stops after {OCR_MAX_PAGES} pages"
            f" or {OCR_SECONDS // 60} minutes"
        )
    return read, None


async def _apdf_text(content: bytes) -> str:
    from fighthealthinsurance.pdf_text import aextract_pdf_page_texts

    try:
        pages = await aextract_pdf_page_texts(
            content, MAX_PAGES, MAX_CHARS, READ_SECONDS, executor=_READERS
        )
    except ValueError:
        # The child's own report: locked, broken or too big to read.
        return ""
    # Pages too short to judge alone are judged together: shifted letters
    # spread over many short pages are garbage only as a group.
    retry = [n for n, page in enumerate(pages) if is_garbled(page)]
    short = [
        n
        for n, page in enumerate(pages)
        if page.strip() and n not in retry and not is_clearly_text(page)
    ]
    doubtful: set[int] = set()
    if short and is_garbled("\n".join(pages[n] for n in short)):
        doubtful = set(short)
        retry = sorted(set(retry) | doubtful)
    read: dict[int, Optional[str]]
    try:
        read, why = await _aocr(content, retry) if retry else ({}, None)
    except (TimeoutError, RuntimeError, OSError) as e:
        if len(retry) == len(pages):
            # Nothing to keep without OCR: the attempt fails and is retried.
            raise
        # Keep the pages the text layer read well, rather than lose them all
        # when every retry times out the same way.
        logger.warning(
            f"OCR of a shared plan document did not finish: {type(e).__name__}"
        )
        read, why = {}, "OCR did not finish"

    def text_of(number: int, page: str) -> str:
        if number in read:
            return (read[number] or "").strip()
        # Not reached: a garbled page stays out, and so does a short page
        # that was garbage with the others.
        return "" if number in doubtful else page.strip()

    texts = [text_of(number, page) for number, page in enumerate(pages)]
    kept = [number for number, text in enumerate(texts) if not is_garbled(text)]
    if not kept or is_garbled("\n".join(texts[number] for number in kept)):
        return ""
    parts = [f"[Page {number + 1}]\n{texts[number]}" for number in kept]
    # A blank page (no text, and nothing for OCR to read) is left out quietly;
    # a page with garbled text, or one OCR failed on or did not reach, is
    # counted in a note.
    unread = [
        number
        for number in set(range(len(pages))) - set(kept)
        if pages[number].strip()
        or read.get(number) is None
        or (read[number] or "").strip()
    ]
    if unread:
        note = (
            f"[{len(unread)} of {len(pages)} pages could not be read and are left out"
        )
        parts.append(note + (f": {why}" if why else "") + ".]")
    return "\n\n".join(parts)


async def aread_text(ext: str, content: bytes) -> str:
    """The text of a stored file with extension ``ext``, or "" when none
    can be read."""
    loop = asyncio.get_running_loop()
    if ext == ".pdf":
        return await _apdf_text(content)
    if ext == ".docx":
        from fighthealthinsurance import word_text
        from fighthealthinsurance.ml.ml_document_extraction import (
            MAX_DOCX_UNPACKED_BYTES,
        )

        text, status = await loop.run_in_executor(
            _READERS,
            word_text.docx_text,
            content,
            MAX_CHARS,
            READ_SECONDS,
            MAX_DOCX_UNPACKED_BYTES,
        )
        return "" if status == word_text.UNREADABLE else text
    if ext == ".txt":
        return content[: MAX_CHARS * 4].decode("utf-8", "replace")[:MAX_CHARS]
    return ""


def clean(text: str) -> str:
    """The text with personal details taken out, within MAX_CHARS."""
    from fighthealthinsurance.plan_document_scrub import scrub

    return scrub(text.strip())[:MAX_CHARS].strip()


async def _astore(document_id: str, status: str, cleaned_text: str) -> str:
    """Store a reading on a row still waiting; report the row's status."""
    stored = await (
        _shared_rows()
        .filter(pk=document_id, processing_status=PolicyDocument.PROCESSING_RECEIVED)
        .aupdate(
            processing_status=status,
            processed_at=timezone.now(),
            cleaned_text=cleaned_text,
        )
    )
    return status if stored else await _status_now(document_id)


async def aprocess_document(document_id: str) -> str:
    """Read, clean and store one shared row. Returns its status, or GONE.
    Raises FileNotRead when the stored file can't be read back, and lets a
    PDF read that timed out or could not run raise, so the activity's
    retries apply."""
    doc = await _shared_rows().filter(pk=document_id).afirst()
    if doc is None:
        return GONE
    if doc.processing_status != PolicyDocument.PROCESSING_RECEIVED:
        return doc.processing_status or GONE
    from fighthealthinsurance.chat_forms import policy_document_extension

    loop = asyncio.get_running_loop()
    content = await loop.run_in_executor(_READERS, _read_stored, doc)
    if not content:
        raise FileNotRead()
    ext = policy_document_extension(doc.filename or doc.document_enc.name or "")
    text = await aread_text(ext, content)
    del content
    if text and is_garbled(text):
        text = ""
    cleaned = await loop.run_in_executor(_READERS, clean, text) if text else ""
    status = (
        PolicyDocument.PROCESSING_READY
        if cleaned
        else PolicyDocument.PROCESSING_UNREADABLE
    )
    stored = await _astore(document_id, status, cleaned)
    if stored == PolicyDocument.PROCESSING_READY and text:
        # The company is read from the text before cleaning, which may have
        # taken a line that looked personal.
        await alink_company(document_id, text)
    return stored


async def afind_company(text: str):
    """The company ``text`` names as plan sponsor: our model's reading,
    checked against the text (plan_sponsor_reader), or, when no model gave
    a usable answer, the fixed patterns' (plan_sponsors.identify_sponsor).
    None when the document names none."""
    from fighthealthinsurance.plan_sponsor_reader import aread_sponsor
    from fighthealthinsurance.plan_sponsors import identify_sponsor

    sponsor, answered = await aread_sponsor(text)
    if answered:
        return sponsor
    return await database_sync_to_async(identify_sponsor)(text)


async def alink_company(document_id: str, text: str) -> None:
    """Link the shared row to the company its text names as plan sponsor,
    adding the company to the list if it's new. A failure is logged and
    leaves the row unlinked: the reading itself stands."""
    from fighthealthinsurance.plan_sponsors import link_sponsor

    try:
        sponsor = await afind_company(text)
        if sponsor is not None:
            await database_sync_to_async(link_sponsor)(document_id, sponsor)
    except Exception:
        logger.opt(exception=True).warning(
            f"Could not link policy document {document_id} to a company"
        )


async def amark_unreadable(document_id: str) -> str:
    """Mark a row still waiting as unreadable, with no text."""
    return await _astore(document_id, PolicyDocument.PROCESSING_UNREADABLE, "")

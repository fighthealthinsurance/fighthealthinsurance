"""Reading shared plan documents, away from the request that stored them.

Each shared file is read once. Its text is pulled out with the bounded
readers (a PDF in a child process, a Word file in one pass, a text file
decoded), personal details are taken out as best we can
(plan_document_scrub), and the cleaned text is stored on the row with where
its reading ended: ready for review, or unreadable when no text could be
read, as with a scanned PDF. The raw text is never stored, and nothing read
from a file is logged or raised.

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
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from django.utils import timezone

from fighthealthinsurance.models import PolicyDocument

#: What a reading reports for a row that is no longer there.
GONE = "gone"

# The staff text view's caps, with fewer characters, since this text is kept.
MAX_PAGES = 1500
MAX_CHARS = 2_000_000
READ_SECONDS = 60
# The encrypted file is about a third bigger than the 20MB upload limit.
MAX_STORED_BYTES = 32 * 1024 * 1024
# More rows than one share can hold (ten files on the share page).
MAX_ROWS_PER_BATCH = 50

# Threads of its own for reading files, so these never wait behind, or hold
# up, the PDF reads appeal generation makes in the same worker process.
_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fhi-plan-docs-read")


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


async def _apdf_text(content: bytes) -> str:
    from fighthealthinsurance.pdf_text import aextract_pdf_page_texts

    try:
        pages = await aextract_pdf_page_texts(
            content, MAX_PAGES, MAX_CHARS, READ_SECONDS, executor=_READERS
        )
    except ValueError:
        # The child's own report: locked, broken or too big to read.
        return ""
    return "\n\n".join(
        f"[Page {number}]\n{page.strip()}"
        for number, page in enumerate(pages, start=1)
        if page.strip()
    )


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
    cleaned = await loop.run_in_executor(_READERS, clean, text) if text else ""
    status = (
        PolicyDocument.PROCESSING_READY
        if cleaned
        else PolicyDocument.PROCESSING_UNREADABLE
    )
    return await _astore(document_id, status, cleaned)


async def amark_unreadable(document_id: str) -> str:
    """Mark a row still waiting as unreadable, with no text."""
    return await _astore(document_id, PolicyDocument.PROCESSING_UNREADABLE, "")

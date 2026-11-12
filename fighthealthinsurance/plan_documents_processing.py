"""Reading shared plan documents, away from the request that stored them.

Each shared file is read once. Its text is pulled out with the bounded
readers (a PDF in a child process, a Word file in one pass, a text file
decoded), personal details are taken out as best we can
(plan_document_scrub), and the cleaned text is stored on the row with where
its reading ended: ready for review, or unreadable when no real text could
be read. The raw text is never stored, and nothing read from a file is
logged or raised.

Text that reads as a person's own papers (plan_document_kind: a bill, an
EOB, a letter about them) keeps no text at all, only its encrypted file;
text with no health plan words is kept but marked not a plan. Neither is
looked up for a company. Staff can say a file is a plan, and its next
reading then skips this check.

A plan's text is read for its company (alink_company), and in the same
model reading for its insurer, plan year and plan type, which fill only
the ones the row has empty (afill_plan_facts). A lookup that does not
finish is counted on the row, and the hourly sweep looks again
(aretry_company_lookups).

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
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Optional

from channels.db import database_sync_to_async
from django.db.models import F, Q
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_document_kind import OTHER, PERSONAL, PLAN, document_kind
from fighthealthinsurance.plan_sponsors import SOURCE_MODEL, SOURCE_STAFF
from fighthealthinsurance.text_quality import (
    is_clearly_text,
    is_garbled,
    real_word_count,
)
from fighthealthinsurance.upload_limits import (
    MAX_OCR_IMAGE_PIXELS,
    MAX_STORED_POLICY_DOCUMENT_BYTES,
    OCR_SECONDS_PER_IMAGE,
)

if TYPE_CHECKING:
    from fighthealthinsurance.plan_sponsor_reader import PlanFacts, PlanReading

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
#: A reading that fails every try (the file can't be fetched, or it times
#: out) leaves the row waiting; the sweep reads it again READ_RETRY_AFTER
#: later, and after MAX_READ_ATTEMPTS such readings, about two days, it is
#: unreadable for good with the reason of the last one.
READ_RETRY_AFTER = datetime.timedelta(hours=6)
MAX_READ_ATTEMPTS = 8
#: A second report of the same failed reading (its bookkeeping activity
#: retried) within this is not counted again. Shorter than READ_RETRY_AFTER,
#: so a real next reading always counts.
SAME_READING = datetime.timedelta(hours=1)
# OCR for pages with no text or garbled text: the first OCR_MAX_PAGES such
# pages, in OCR_SECONDS in all, each drawn at OCR_DPI within the server OCR's
# pixel cap and time per image. Pages past either bound are left out, and the
# text says how many.
OCR_MAX_PAGES = 40
OCR_SECONDS = 120
OCR_DPI = 200
# A page whose text layer has fewer visible characters than this (a scanner's
# stamp, a fax header, "Page 3") is read with OCR too, and keeps whichever
# reading has more real words, so a short cover page is never lost.
THIN_PAGE_CHARS = 100

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


def _due_rows():
    # Waiting rows, less those whose last reading failed too recently.
    return _shared_rows().filter(
        Q(read_attempted_at__isnull=True)
        | Q(read_attempted_at__lte=timezone.now() - READ_RETRY_AFTER),
        processing_status=PolicyDocument.PROCESSING_RECEIVED,
    )


async def abatch_document_ids(share_batch: str) -> list[str]:
    """The ids of the batch's rows waiting to be read and due, oldest first."""
    rows = (
        _due_rows()
        .filter(share_batch=share_batch)
        .order_by("shared_at", "id")
        .values_list("pk", flat=True)[:MAX_ROWS_PER_BATCH]
    )
    return [str(pk) async for pk in rows]


async def awaiting_batches(received_minutes: int, limit: int) -> list[str]:
    """Up to ``limit`` share batches, oldest first, holding a row shared
    more than ``received_minutes`` ago that is still waiting to be read."""
    cutoff = timezone.now() - datetime.timedelta(minutes=received_minutes)
    rows = (
        _due_rows()
        .filter(
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


#: Reasons a reading returns that are worth reading again later.
_READ_AGAIN_LATER = (
    PolicyDocument.UNREADABLE_OCR_UNAVAILABLE,
    PolicyDocument.UNREADABLE_DID_NOT_FINISH,
)
# Why OCR left pages unread, as a reason for a file with nothing kept.
_OCR_REASONS = {
    "OCR could not run": PolicyDocument.UNREADABLE_OCR_UNAVAILABLE,
    "OCR could not open the file": PolicyDocument.UNREADABLE_CANNOT_OPEN,
    "OCR did not finish": PolicyDocument.UNREADABLE_DID_NOT_FINISH,
}


async def _apdf_text(content: bytes) -> tuple[str, str]:
    from fighthealthinsurance.pdf_text import aextract_pdf_page_texts

    try:
        pages = await aextract_pdf_page_texts(
            content, MAX_PAGES, MAX_CHARS, READ_SECONDS, executor=_READERS
        )
    except ValueError:
        # The child's own report: locked, broken or too big to read.
        return "", PolicyDocument.UNREADABLE_CANNOT_OPEN
    if not pages:
        return "", PolicyDocument.UNREADABLE_CANNOT_OPEN
    # Pages too short to judge alone are judged together: shifted letters
    # spread over many short pages are garbage only as a group.
    garbled = {n for n, page in enumerate(pages) if is_garbled(page)}
    short = [
        n
        for n, page in enumerate(pages)
        if page.strip() and n not in garbled and not is_clearly_text(page)
    ]
    doubtful: set[int] = set()
    if short and is_garbled("\n".join(pages[n] for n in short)):
        doubtful = set(short)
    thin = {
        n
        for n, page in enumerate(pages)
        if 0 < sum(1 for ch in page if not ch.isspace()) < THIN_PAGE_CHARS
    }
    unusable = garbled | doubtful
    retry = sorted(unusable | thin)
    read: dict[int, Optional[str]]
    try:
        read, why = await _aocr(content, retry) if retry else ({}, None)
    except (TimeoutError, RuntimeError, OSError) as e:
        if len(unusable) == len(pages):
            # Nothing to keep without OCR: the attempt fails and is retried.
            raise
        # Keep the pages the text layer read well, rather than lose them all
        # when every retry times out the same way.
        logger.warning(
            f"OCR of a shared plan document did not finish: {type(e).__name__}"
        )
        read, why = {}, "OCR did not finish"

    def text_of(number: int, page: str) -> str:
        layer = "" if number in unusable else page.strip()
        ocr = read.get(number)
        if ocr is None:
            return layer
        ocr = ocr.strip()
        return ocr if real_word_count(ocr) > real_word_count(layer) else layer

    texts = [text_of(number, page) for number, page in enumerate(pages)]
    kept = [number for number, text in enumerate(texts) if not is_garbled(text)]
    if not kept or is_garbled("\n".join(texts[number] for number in kept)):
        if not retry:
            return "", PolicyDocument.UNREADABLE_GARBLED
        if why in _OCR_REASONS:
            return "", _OCR_REASONS[why]
        if not any(isinstance(read.get(number), str) for number in retry):
            # OCR read no page at all (each failed or was not reached): a
            # failure to read again later, not a scan with nothing on it.
            return "", PolicyDocument.UNREADABLE_DID_NOT_FINISH
        return "", PolicyDocument.UNREADABLE_NOTHING_AFTER_OCR
    parts = [f"[Page {number + 1}]\n{texts[number]}" for number in kept]
    # A blank page (no text, and nothing for OCR to read) is left out quietly.
    # A page left out with text on it, or one OCR failed on or did not reach,
    # is counted in a note, and so is a thin page kept with only its text
    # layer because OCR could not read it.
    unread = [
        number
        for number in range(len(pages))
        if (
            number not in kept
            and (
                pages[number].strip()
                or read.get(number) is None
                or (read[number] or "").strip()
            )
        )
        or (number in kept and number in thin and read.get(number) is None)
    ]
    if unread:
        note = f"[{len(unread)} of {len(pages)} pages could not be read"
        parts.append(note + (f": {why}" if why else "") + ".]")
    return "\n\n".join(parts), ""


async def aread_text(ext: str, content: bytes) -> tuple[str, str]:
    """(The text of a stored file with extension ``ext``, ""), or ("", why
    none could be read: a PolicyDocument.UNREADABLE_* reason)."""
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
        if status == word_text.UNREADABLE:
            return "", PolicyDocument.UNREADABLE_CANNOT_OPEN
        return text, ""
    if ext == ".txt":
        return content[: MAX_CHARS * 4].decode("utf-8", "replace")[:MAX_CHARS], ""
    return "", PolicyDocument.UNREADABLE_CANNOT_OPEN


def clean(text: str) -> str:
    """The text with personal details taken out, within MAX_CHARS."""
    from fighthealthinsurance.plan_document_scrub import scrub

    return scrub(text.strip())[:MAX_CHARS].strip()


async def _astore(
    document_id: str, status: str, cleaned_text: str, **cleared: Any
) -> str:
    """Store a reading on a row still waiting; report the row's status."""
    stored = await (
        _shared_rows()
        .filter(pk=document_id, processing_status=PolicyDocument.PROCESSING_RECEIVED)
        .aupdate(
            processing_status=status,
            processed_at=timezone.now(),
            cleaned_text=cleaned_text,
            **cleared,
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
    try:
        content = await loop.run_in_executor(_READERS, _read_stored, doc)
        if not content:
            raise FileNotRead()
    except Exception:
        await _anote_failure(document_id, PolicyDocument.UNREADABLE_FILE_NOT_READ)
        raise
    ext = policy_document_extension(doc.filename or doc.document_enc.name or "")
    try:
        text, reason = await aread_text(ext, content)
    except Exception:
        await _anote_failure(document_id, PolicyDocument.UNREADABLE_DID_NOT_FINISH)
        raise
    del content
    if not text and reason in _READ_AGAIN_LATER:
        # Ours to fix, not the file's (a missing tesseract, OCR that read no
        # page): it waits to be read again like a reading that raised.
        await _anote_failure(document_id, reason)
        return await amark_unreadable(document_id)
    if text and is_garbled(text):
        text, reason = "", PolicyDocument.UNREADABLE_GARBLED
    kind = PLAN
    if text and not doc.staff_says_plan:
        kind = await loop.run_in_executor(_READERS, document_kind, text)
        logger.info(f"Shared plan document {document_id} reads as {kind}")
    cleared: dict[str, Any] = {"unreadable_reason": ""}
    if kind == PERSONAL:
        # A person's own papers: the file stays, encrypted; no text is kept,
        # nor a line an earlier reading quoted for its company.
        status, cleaned = PolicyDocument.PROCESSING_PERSONAL, ""
        cleared["plan_sponsor_evidence"] = ""
    else:
        cleaned = await loop.run_in_executor(_READERS, clean, text) if text else ""
        if not cleaned:
            status = PolicyDocument.PROCESSING_UNREADABLE
            cleared["unreadable_reason"] = reason or PolicyDocument.UNREADABLE_GARBLED
            logger.info(
                f"Shared plan document {document_id} is unreadable: "
                f"{cleared['unreadable_reason']}"
            )
        elif kind == OTHER:
            status = PolicyDocument.PROCESSING_NOT_A_PLAN
        else:
            status = PolicyDocument.PROCESSING_READY
    stored = await _astore(document_id, status, cleaned, **cleared)
    if stored == PolicyDocument.PROCESSING_READY and text:
        # The company is read from the text before cleaning, which may have
        # taken a line that looked personal.
        await alink_company(document_id, text, cleaned)
    return stored


async def afind_company(text: str, cleaned_text: str) -> "PlanReading":
    """What ``text`` says about its plan. Its sponsor, the company it names
    (None when it names none): our model's reading, checked against the
    text (plan_sponsor_reader), or, when no model gave a usable answer, the
    fixed patterns' (plan_sponsors.identify_sponsor). Its insurer, plan year
    and plan type: our model's reading only, each checked against
    ``cleaned_text``, the text as stored."""
    from fighthealthinsurance.plan_sponsor_reader import aread_plan
    from fighthealthinsurance.plan_sponsors import identify_sponsor

    reading = await aread_plan(text, cleaned_text)
    if reading.answered:
        return reading
    sponsor = await database_sync_to_async(identify_sponsor)(text)
    return reading._replace(sponsor=sponsor)


#: The plan facts a reading may fill, each only while it is empty: never
#: one the person gave, or one staff set or cleared.
EMPTY_PLAN_FACTS = {
    "insurer_name": Q(insurer_name="") & ~Q(insurer_name_source=SOURCE_STAFF),
    "plan_year": Q(plan_year__isnull=True) & ~Q(plan_year_source=SOURCE_STAFF),
    "plan_category": Q(plan_category__in=("", "unknown"))
    & ~Q(plan_category_source=SOURCE_STAFF),
}


async def afill_plan_facts(document_id: Any, facts: "PlanFacts") -> list[str]:
    """Fill each fact in ``facts`` that the shared row has empty
    (EMPTY_PLAN_FACTS), marked as our model's, while the row is read as
    ready. Returns the fields filled."""
    filled = []
    for field, empty in EMPTY_PLAN_FACTS.items():
        value = getattr(facts, field)
        if value and await ready_row(document_id).filter(empty).aupdate(
            **{field: value, f"{field}_source": SOURCE_MODEL}
        ):
            filled.append(field)
    return filled


#: A company lookup that does not finish (lookup_finished) is counted on the
#: row, and the hourly sweep looks again (aretry_company_lookups) until
#: MAX_COMPANY_LOOKUPS have not finished, about seven hours; the row is then
#: left for staff (link_plan_sponsors). A run looks again at
#: MAX_LOOKUPS_PER_RUN rows at most and starts none after LOOKUP_RUN_SECONDS.
MAX_COMPANY_LOOKUPS = 8
MAX_LOOKUPS_PER_RUN = 5
LOOKUP_RUN_SECONDS = 120


def lookup_finished(reading: "PlanReading") -> bool:
    """Whether a reading settled the company: a model answered, or the fixed
    patterns found one. Not when no model answered and the patterns found
    none: that is a lookup to try again, not a document naming no company."""
    return reading.answered or reading.sponsor is not None


async def alink_company(document_id: str, text: str, cleaned_text: str) -> bool:
    """Link the shared row to the company its text names as plan sponsor,
    adding the company to the list if it's new, and fill the insurer, plan
    year and plan type our model found where the row has none
    (afill_plan_facts), checked against ``cleaned_text``, the text as
    stored. True when the lookup finished. One that did not (an
    error, or not lookup_finished) is counted on the row
    (company_lookup_failures); the reading itself stands."""
    from fighthealthinsurance.plan_sponsors import link_sponsor

    try:
        found = await afind_company(text, cleaned_text)
        if found.sponsor is not None:
            await database_sync_to_async(link_sponsor)(document_id, found.sponsor)
        await afill_plan_facts(document_id, found.facts)
    except Exception as e:
        # The class only: an error's message can quote the document.
        logger.warning(
            f"Could not link policy document {document_id} to a company: "
            f"{type(e).__name__}"
        )
        await _acount_lookup_failure(document_id)
        return False
    if not lookup_finished(found):
        logger.info(
            f"Company lookup for policy document {document_id} did not finish: "
            "no model answered"
        )
        await _acount_lookup_failure(document_id)
        return False
    await ready_row(document_id).filter(company_lookup_failures__gt=0).aupdate(
        company_lookup_failures=0
    )
    return True


async def _acount_lookup_failure(document_id: Any) -> None:
    # Counted apart from "names no company", so the sweep and staff look again.
    await ready_row(document_id).aupdate(
        company_lookup_failures=F("company_lookup_failures") + 1
    )


def _lookups_due():
    # Fewest failed first, so one that keeps failing does not hold the rest.
    return (
        _shared_rows()
        .filter(
            processing_status=PolicyDocument.PROCESSING_READY,
            company_lookup_failures__gt=0,
            company_lookup_failures__lt=MAX_COMPANY_LOOKUPS,
        )
        .exclude(plan_sponsor_source=SOURCE_STAFF)
        .order_by("company_lookup_failures", "id")
    )


async def aretry_company_lookups(limit: int = MAX_LOOKUPS_PER_RUN) -> int:
    """Look again, from their cleaned text, for the companies of up to
    ``limit`` shared rows whose lookup did not finish, fewer than
    MAX_COMPANY_LOOKUPS times. Starts none after LOOKUP_RUN_SECONDS, and
    stops at the first that does not finish: the models are likely down,
    and the rest keep their tries. Returns how many finished."""
    started = time.monotonic()
    ids = [pk async for pk in _lookups_due().values_list("pk", flat=True)[:limit]]
    finished = 0
    for pk in ids:
        text = await ready_row(pk).values_list("cleaned_text", flat=True).afirst()
        if not text:
            continue
        # Checked just before the lookup, so none starts past the bound.
        if time.monotonic() - started >= LOOKUP_RUN_SECONDS:
            logger.info("Company lookups hit the run's time bound; the rest wait")
            break
        # The stored text is the cleaned one, so it is both.
        if not await alink_company(pk, text, text):
            logger.info("A company lookup did not finish; the rest wait")
            break
        finished += 1
    if ids:
        logger.info(f"Company lookups looked at again: {finished} of {len(ids)}")
    return finished


def ready_row(document_id: Any):
    """The shared row, only while it is read as ready: one removed or marked
    spam meanwhile is left as it is."""
    return _shared_rows().filter(
        pk=document_id, processing_status=PolicyDocument.PROCESSING_READY
    )


async def _anote_failure(document_id: str, reason: str) -> None:
    # Why the reading failed, on a row still waiting, for when it gives up.
    await _shared_rows().filter(
        pk=document_id, processing_status=PolicyDocument.PROCESSING_RECEIVED
    ).aupdate(unreadable_reason=reason)


async def amark_unreadable(document_id: str) -> str:
    """For a row whose reading failed every try: it waits READ_RETRY_AFTER
    for the sweep to read it again, and after MAX_READ_ATTEMPTS such
    readings it is unreadable for good, with no text and the reason of the
    last failure."""
    now = timezone.now()
    # Not a reading already counted: a retried activity reports it once.
    waiting = _shared_rows().filter(
        Q(read_attempted_at__isnull=True)
        | Q(read_attempted_at__lte=now - SAME_READING),
        pk=document_id,
        processing_status=PolicyDocument.PROCESSING_RECEIVED,
    )
    retried = await waiting.filter(read_attempts__lt=MAX_READ_ATTEMPTS - 1).aupdate(
        read_attempts=F("read_attempts") + 1, read_attempted_at=now
    )
    if retried:
        return PolicyDocument.PROCESSING_RECEIVED
    doc = await waiting.only("unreadable_reason").afirst()
    if doc is None:
        # Counted already, removed, or read meanwhile.
        return await _status_now(document_id)
    reason = doc.unreadable_reason or PolicyDocument.UNREADABLE_DID_NOT_FINISH
    logger.info(f"Shared plan document {document_id} is unreadable: {reason}")
    return await _astore(
        document_id,
        PolicyDocument.PROCESSING_UNREADABLE,
        "",
        unreadable_reason=reason,
        read_attempts=F("read_attempts") + 1,
        read_attempted_at=now,
    )

"""
Document processing for chat-uploaded files.

Handles chunking large documents and summarizing each chunk using ML models,
so that the full document text doesn't need to sit in the chat history.
"""

import asyncio
import math
import re
import threading
from datetime import timedelta
from typing import Dict, List, Optional

from django.utils import timezone
from loguru import logger

from fighthealthinsurance.ml.ml_inference import infer_with_fallback
from fighthealthinsurance.models import ChatDocument
from fighthealthinsurance.utils import fire_and_forget_in_new_threadpool

DEFAULT_CHUNK_SIZE = 4000
DEFAULT_OVERLAP = 500
SUMMARIZE_TIMEOUT = 30
OVERALL_SUMMARY_TIMEOUT = 45
CHUNK_BATCH_SIZE = 3
MIN_VALID_SUMMARY_CHARS = 20
# Models tried (sequentially, each under its own timeout) per summary call;
# part of the hard bound computed by max_summarization_seconds.
SUMMARY_MODEL_ATTEMPTS = 3


def chunk_document(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[Dict]:
    """Split document text into overlapping chunks, breaking at paragraph boundaries."""
    if not text or not text.strip():
        return []

    if len(text) <= chunk_size:
        return [
            {"chunk_index": 0, "start_char": 0, "end_char": len(text), "text": text}
        ]

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))

        if end < len(text):
            para_break = text.rfind("\n\n", start + chunk_size // 2, end)
            if para_break > start:
                end = para_break + 2
            else:
                line_break = text.rfind("\n", start + chunk_size // 2, end)
                if line_break > start:
                    end = line_break + 1
                else:
                    sentence_end = -1
                    for match in re.finditer(
                        r"\.\s", text[start + chunk_size // 2 : end]
                    ):
                        sentence_end = start + chunk_size // 2 + match.start() + 1
                    if sentence_end > start:
                        end = sentence_end + 1

        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "start_char": start,
                    "end_char": end,
                    "text": chunk_text,
                }
            )
            chunk_index += 1

        if end >= len(text):
            break

        start = max(end - overlap, start + 1)

    return chunks


async def _try_internal_models(
    system_prompt: str,
    prompt: str,
    timeout: float,
    min_length: int = MIN_VALID_SUMMARY_CHARS,
    temperature: float = 0.3,
) -> Optional[str]:
    """Try up to SUMMARY_MODEL_ATTEMPTS internal models sequentially, returning
    the first valid result."""
    return await infer_with_fallback(
        system_prompts=[system_prompt],
        prompt=prompt,
        temperature=temperature,
        timeout=timeout,
        model_count=SUMMARY_MODEL_ATTEMPTS,
        min_length=min_length,
        label="doc chunk summary",
    )


async def _summarize_single_chunk(
    chunk_text: str,
    chunk_index: int,
    total_chunks: int,
    denial_context: Optional[str] = None,
) -> Optional[str]:
    """Summarize a single document chunk using internal ML models."""
    context_str = ""
    if denial_context:
        context_str = (
            f"\nContext: This document was uploaded as part of a health insurance "
            f"denial appeal. {denial_context}\n"
        )

    prompt = f"""Summarize the following section (part {chunk_index + 1} of {total_chunks}) of a document uploaded during a health insurance appeal chat session.
{context_str}
Focus on information relevant to health insurance appeals, including:
- Coverage policies, medical necessity criteria
- Appeal procedures, deadlines, rights
- Exclusions, limitations, exceptions
- Prior authorization requirements
- Any medical or clinical information

Document section:
{chunk_text[:DEFAULT_CHUNK_SIZE]}

Provide a concise summary (max 200 words) capturing the key information from this section."""

    return await _try_internal_models(
        system_prompt=(
            "You are an expert at analyzing health insurance documents. "
            "Provide concise, accurate summaries focused on information "
            "relevant to insurance appeals."
        ),
        prompt=prompt,
        timeout=SUMMARIZE_TIMEOUT,
    )


async def _generate_overall_summary(
    chunk_summaries: List[str],
    document_name: str,
    denial_context: Optional[str] = None,
) -> Optional[str]:
    """Generate an overall document summary from chunk summaries."""
    combined = "\n\n".join(
        f"Section {i + 1}: {s}" for i, s in enumerate(chunk_summaries) if s
    )
    if not combined:
        return None

    context_str = f"\nContext: {denial_context}\n" if denial_context else ""

    prompt = f"""Create a brief overall summary of the document "{document_name}" based on these section summaries.
{context_str}
Section summaries:
{combined[:6000]}

Provide a concise overall summary (max 300 words) that captures the most important information
from this document relevant to a health insurance denial appeal."""

    result = await _try_internal_models(
        system_prompt=(
            "You are an expert at summarizing health insurance documents for appeal purposes."
        ),
        prompt=prompt,
        timeout=OVERALL_SUMMARY_TIMEOUT,
    )
    if result:
        return result

    # Fallback: concatenate chunk summaries if all models fail
    return f"Document: {document_name}\n\n{combined[:2000]}"


async def summarize_chunks(
    chat_document_id: int,
    denial_context: Optional[str] = None,
) -> None:
    """Background task: chunk and summarize a ChatDocument.

    Callers must already hold the document's claim (see
    _claim_document_for_processing), which is what marks it PROCESSING.
    """
    try:
        doc = await ChatDocument.objects.aget(id=chat_document_id)
    except ChatDocument.DoesNotExist:
        logger.warning(f"ChatDocument {chat_document_id} not found for summarization")
        return

    try:
        chunks = chunk_document(doc.full_text)
        if not chunks:
            doc.processing_status = ChatDocument.Status.COMPLETED
            doc.summary = "(Empty document)"
            await doc.asave(update_fields=["processing_status", "summary"])
            return

        chunk_results: List[Dict] = []
        for batch_start in range(0, len(chunks), CHUNK_BATCH_SIZE):
            batch = chunks[batch_start : batch_start + CHUNK_BATCH_SIZE]
            summaries = await asyncio.gather(
                *(
                    _summarize_single_chunk(
                        chunk["text"],
                        chunk["chunk_index"],
                        len(chunks),
                        denial_context,
                    )
                    for chunk in batch
                ),
                return_exceptions=True,
            )
            for chunk, summary in zip(batch, summaries):
                chunk_results.append(
                    {
                        "chunk_index": chunk["chunk_index"],
                        "start_char": chunk["start_char"],
                        "end_char": chunk["end_char"],
                        "summary": str(summary) if isinstance(summary, str) else "",
                    }
                )

        successful_summaries = [c["summary"] for c in chunk_results if c.get("summary")]
        doc.chunk_summaries = chunk_results

        if not successful_summaries:
            doc.processing_status = ChatDocument.Status.FAILED
            await doc.asave(update_fields=["chunk_summaries", "processing_status"])
            logger.warning(
                f"No chunk summaries generated for ChatDocument {chat_document_id} "
                f"({len(chunks)} chunks attempted)"
            )
            return

        overall = await _generate_overall_summary(
            successful_summaries, doc.document_name, denial_context
        )
        if overall:
            doc.summary = overall

        doc.processing_status = ChatDocument.Status.COMPLETED
        await doc.asave(
            update_fields=["chunk_summaries", "summary", "processing_status"]
        )
        logger.info(
            f"Completed summarization of ChatDocument {chat_document_id} "
            f"({len(chunks)} chunks, {len(successful_summaries)} summarized)"
        )

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Failed to summarize ChatDocument {chat_document_id}: "
            f"{type(e).__name__}"
        )
        try:
            doc.processing_status = ChatDocument.Status.FAILED
            await doc.asave(update_fields=["processing_status"])
        except Exception as save_err:
            logger.debug(f"Could not persist failed status: {type(save_err).__name__}")


def max_summarization_seconds(full_text: str) -> float:
    """Hard upper bound on how long one summarize_chunks run can take.

    Every model call it makes goes through infer_with_fallback, which tries at
    most SUMMARY_MODEL_ATTEMPTS models, each under its own wait_for. A batch's
    chunks run concurrently, so each batch costs at most one call's worth, and
    the overall summary one more.
    """
    batches = math.ceil(len(chunk_document(full_text)) / CHUNK_BATCH_SIZE)
    return SUMMARY_MODEL_ATTEMPTS * (
        batches * SUMMARIZE_TIMEOUT + OVERALL_SUMMARY_TIMEOUT
    )


# Statuses a summarization worker may claim a document from: fresh, or its
# last analysis failed (resubmitting the content retries it). Never
# PROCESSING -- see _claim_document_for_processing.
_CLAIMABLE_STATUSES = (ChatDocument.Status.PENDING, ChatDocument.Status.FAILED)

# How long a worker waits for its release (see process_uploaded_document)
# before summarizing anyway: a backstop for a turn wedged far past its
# budget, not the normal path.
MAX_SUMMARIZATION_WAIT_SECONDS = 600.0


def _abandonment_window_seconds(full_text: str) -> float:
    """Age past which a PROCESSING document has no live worker.

    A document's worker claims it no later than MAX_SUMMARIZATION_WAIT_SECONDS
    after it is stored, then runs no longer than max_summarization_seconds,
    so one still PROCESSING past their sum (plus a minute of slack for the
    database work around the model calls) was abandoned -- typically its
    worker died with a pod restart, which takes fire-and-forget threads with
    it. Reusing it would pin every re-paste to a document nothing will ever
    analyze.

    One case outlives the window: a FAILED document retried by a re-paste
    long after it was first stored. A further re-paste during that retry
    stores a fresh copy -- one duplicate analysis, which is what every
    re-paste did before documents were deduplicated at all.
    """
    return MAX_SUMMARIZATION_WAIT_SECONDS + max_summarization_seconds(full_text) + 60


async def _claim_document_for_processing(doc_id: int) -> bool:
    """Atomically claim ``doc_id`` for summarization (-> PROCESSING).

    A conditional UPDATE, so of the workers waiting on one document (its
    content was resubmitted while the first still waited) only one runs at a
    time: a later one finds it claimed and exits, unless the earlier analysis
    FAILED, which it then retries.

    Never claim FROM PROCESSING: PROCESSING -> PROCESSING changes nothing but
    still MATCHES, so the database reports a successful claim to every caller
    at once. An abandoned PROCESSING document is not reclaimed at all --
    deduplication stores a fresh copy instead (see _reusable_copy).
    """
    updated = await ChatDocument.objects.filter(
        id=doc_id, processing_status__in=_CLAIMABLE_STATUSES
    ).aupdate(processing_status=ChatDocument.Status.PROCESSING)
    return bool(updated)


async def _summarize_when_released(
    doc_id: int,
    denial_context: Optional[str],
    release: Optional[threading.Event],
) -> None:
    """Background worker: once ``release`` is set (at once when None), claim
    the document and summarize it.

    Runs alone on the private event loop of its own fire-and-forget thread,
    so the blocking wait stalls nothing else.
    """
    if release is not None and not release.wait(MAX_SUMMARIZATION_WAIT_SECONDS):
        logger.warning(
            f"ChatDocument {doc_id} was not released within "
            f"{MAX_SUMMARIZATION_WAIT_SECONDS:.0f}s; summarizing it anyway"
        )
    # Filtered by id, so a document deleted meanwhile just loses the claim.
    if await _claim_document_for_processing(doc_id):
        await summarize_chunks(doc_id, denial_context=denial_context)


async def _reusable_copy(chat, full_text: str) -> Optional[ChatDocument]:
    """The newest stored copy of ``full_text`` in ``chat`` worth reusing.

    Every copy is, except one abandoned mid-analysis: still PROCESSING past
    its abandonment window (see _abandonment_window_seconds), which no worker
    will ever finish, so the caller stores and analyzes a fresh copy instead.
    A PENDING or FAILED copy is reused and handed a new worker.

    One query, compared in the database: only the bookkeeping columns come
    back, never the (possibly multi-megabyte) text the caller already holds.
    A reused document is returned with just those columns loaded.
    """
    now = timezone.now()
    window: Optional[float] = None
    async for candidate in (
        ChatDocument.objects.filter(
            chat=chat, char_count=len(full_text), full_text=full_text
        )
        .only("id", "chat_id", "document_name", "processing_status", "created_at")
        .order_by("-created_at")
    ):
        if candidate.processing_status != ChatDocument.Status.PROCESSING:
            return candidate
        if window is None:
            window = _abandonment_window_seconds(full_text)
        if candidate.created_at > now - timedelta(seconds=window):
            return candidate
        logger.warning(
            f"Not reusing ChatDocument {candidate.id} for chat {chat.id}: still "
            f"processing past its {window:.0f}s abandonment window -- storing "
            f"a fresh copy"
        )
    return None


async def process_uploaded_document(
    chat,
    document_name: str,
    full_text: str,
    denial_context: Optional[str] = None,
    summarize_after: Optional[threading.Event] = None,
) -> ChatDocument:
    """Store ``full_text`` as a ChatDocument and dispatch its background
    summarization. Returns the ChatDocument so the caller can reference it.

    Identical content re-submitted to the same chat (the user re-pasting a
    long message after a failed turn, or re-uploading the same file) reuses
    the existing document -- its original ``document_name`` wins -- instead of
    creating a duplicate row and a second summarization storm, unless that
    document was abandoned mid-analysis (see _reusable_copy).

    The worker starts summarizing once ``summarize_after`` is set, or at once
    without one. The chat turn passes an event it sets when the turn is over,
    however it ends, so the chunk summaries don't compete with the turn's own
    model calls for the same backends.

    Shielded against caller cancellation: once the row is being written, a
    disconnect must not leave it committed with no worker dispatched.
    """
    return await asyncio.shield(
        _store_document(chat, document_name, full_text, denial_context, summarize_after)
    )


async def _store_document(
    chat,
    document_name: str,
    full_text: str,
    denial_context: Optional[str],
    summarize_after: Optional[threading.Event],
) -> ChatDocument:
    doc = await _reusable_copy(chat, full_text)
    if doc is not None:
        # Ids and status only: the document name is client-supplied.
        logger.info(
            f"Reusing ChatDocument {doc.id} for chat {chat.id}: identical "
            f"content re-submitted (status={doc.processing_status})"
        )
    else:
        doc = await ChatDocument.objects.acreate(
            chat=chat,
            document_name=document_name or "uploaded_document",
            full_text=full_text,
            char_count=len(full_text),
            processing_status=ChatDocument.Status.PENDING,
        )
        logger.info(
            f"Created ChatDocument {doc.id} for chat {chat.id} "
            f"({len(full_text)} chars)"
        )

    # A reused copy that is being (or has been) analyzed needs no worker.
    if doc.processing_status in _CLAIMABLE_STATUSES:
        await fire_and_forget_in_new_threadpool(
            _summarize_when_released(doc.id, denial_context, summarize_after)
        )
    return doc

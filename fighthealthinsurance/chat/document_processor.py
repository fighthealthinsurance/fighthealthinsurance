"""
Document processing for chat-uploaded files.

Handles chunking large documents and summarizing each chunk using ML models,
so that the full document text doesn't need to sit in the chat history.
"""

import asyncio
import re
from typing import Dict, List, Optional

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
    """Try the top-3 internal models sequentially, returning the first valid result."""
    return await infer_with_fallback(
        system_prompts=[system_prompt],
        prompt=prompt,
        temperature=temperature,
        timeout=timeout,
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
    """Background task: chunk and summarize a ChatDocument."""
    try:
        doc = await ChatDocument.objects.aget(id=chat_document_id)
    except ChatDocument.DoesNotExist:
        logger.warning(f"ChatDocument {chat_document_id} not found for summarization")
        return

    try:
        doc.processing_status = ChatDocument.Status.PROCESSING
        await doc.asave(update_fields=["processing_status"])

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
            f"Failed to summarize ChatDocument {chat_document_id}: {e}"
        )
        try:
            doc.processing_status = ChatDocument.Status.FAILED
            await doc.asave(update_fields=["processing_status"])
        except Exception as save_err:
            logger.debug(f"Could not persist failed status: {save_err}")


def summarization_needed(doc: ChatDocument) -> bool:
    """Whether ``doc`` still needs (re)summarization kicked off.

    PENDING covers both a fresh document and one whose deferred kickoff never
    ran (rescued by the watchdog below); FAILED documents get another try on
    resubmission. PROCESSING/COMPLETED are left alone.
    """
    return doc.processing_status in (
        ChatDocument.Status.PENDING,
        ChatDocument.Status.FAILED,
    )


async def _claim_document_for_processing(
    doc_id: int,
    from_statuses: List[str],
) -> bool:
    """Atomically claim ``doc_id`` for summarization (-> PROCESSING).

    A conditional UPDATE, so of any number of concurrent callers (the turn's
    deferred kickoff, the storage watchdog, a resubmission) exactly one wins
    and dispatches a worker; the in-memory status checks alone raced.
    """
    updated = await ChatDocument.objects.filter(
        id=doc_id, processing_status__in=from_statuses
    ).aupdate(processing_status=ChatDocument.Status.PROCESSING)
    return bool(updated)


async def start_document_summarization(
    doc: ChatDocument,
    denial_context: Optional[str] = None,
) -> bool:
    """Fire background summarization for ``doc`` if it still needs it.

    Safe to call repeatedly and concurrently: after the cheap in-memory
    pre-filter, the document is claimed with an atomic conditional UPDATE, so
    only one caller dispatches a worker per PENDING/FAILED state. Returns
    True when this call dispatched the background task. If dispatch itself
    fails, the claim is released back to FAILED so a later attempt can retry.
    """
    if not summarization_needed(doc):
        return False
    if not await _claim_document_for_processing(
        doc.id,
        [ChatDocument.Status.PENDING, ChatDocument.Status.FAILED],
    ):
        return False
    doc.processing_status = ChatDocument.Status.PROCESSING
    try:
        await fire_and_forget_in_new_threadpool(
            summarize_chunks(doc.id, denial_context=denial_context)
        )
    except Exception:
        doc.processing_status = ChatDocument.Status.FAILED
        await ChatDocument.objects.filter(
            id=doc.id, processing_status=ChatDocument.Status.PROCESSING
        ).aupdate(processing_status=ChatDocument.Status.FAILED)
        raise
    return True


# How long the watchdog waits before rescuing a still-PENDING deferred
# document. Longer than the chat turn budget (FHI_CHAT_TURN_BUDGET, 150s
# default) so the turn's own kickoff always gets to go first.
DEFERRED_SUMMARY_WATCHDOG_SECONDS = 240.0


async def _deferred_summarization_watchdog(
    doc_id: int,
    denial_context: Optional[str],
    delay: float,
    claim_statuses: Optional[List[str]] = None,
) -> None:
    """Backstop for deferred summarization: rescue a stranded document.

    The turn that stored the document is supposed to kick summarization off
    after its LLM pass, but anything between storage and that kickoff -- a
    raise in history prep, the WebSocket consumer being cancelled on
    disconnect -- skips it. This runs in its own fire-and-forget thread, so
    it survives the consumer coroutine, waits out the turn, and claims the
    document only if it still sits in ``claim_statuses`` -- the status it had
    when the watchdog was armed (default PENDING). A fresh document whose
    fast path already ran therefore is not re-touched, while a FAILED
    document the user explicitly resubmitted still gets its retry even if
    the resubmitting turn dies. (For that resubmitted-FAILED case, a turn
    whose own retry ran and failed again may get one extra watchdog retry --
    bounded to one per resubmission and accepted.)
    """
    if claim_statuses is None:
        claim_statuses = [ChatDocument.Status.PENDING]
    await asyncio.sleep(delay)
    try:
        doc = await ChatDocument.objects.aget(id=doc_id)
    except ChatDocument.DoesNotExist:
        return
    if not await _claim_document_for_processing(doc_id, claim_statuses):
        return
    logger.warning(
        f"ChatDocument {doc_id} was still {'/'.join(claim_statuses)} "
        f"{delay:.0f}s after deferred storage (its turn never started "
        f"summarization); watchdog starting it"
    )
    await summarize_chunks(doc_id, denial_context=denial_context)


async def process_uploaded_document(
    chat,
    document_name: str,
    full_text: str,
    denial_context: Optional[str] = None,
    defer_summarization: bool = False,
) -> ChatDocument:
    """Store ``full_text`` as a ChatDocument and (by default) fire background
    summarization. Returns the ChatDocument so the caller can reference it.

    Identical content re-submitted to the same chat (the user re-pasting a
    long message after a failed turn, or re-uploading the same file) reuses
    the existing document -- its original ``document_name`` wins -- instead of
    creating a duplicate row and a second summarization storm.

    With ``defer_summarization=True`` no summarization worker is dispatched
    here; the caller must call :func:`start_document_summarization` after its
    own LLM pass. The chat turn uses this so the batch summarization work
    doesn't compete with the interactive LLM calls for the same backends. A
    watchdog thread is still armed at storage time so the document cannot be
    stranded PENDING if the caller dies (disconnect, setup error) before its
    deferred kickoff runs -- the atomic claim keeps the two from ever both
    dispatching.
    """
    doc: Optional[ChatDocument] = None
    async for existing in (
        ChatDocument.objects.filter(chat=chat, char_count=len(full_text))
        .order_by("-created_at")
        .aiterator()
    ):
        if existing.full_text == full_text:
            doc = existing
            logger.info(
                f"Reusing ChatDocument {doc.id} ({doc.document_name}) for chat "
                f"{chat.id}: identical content re-submitted "
                f"(status={doc.processing_status})"
            )
            break

    if doc is None:
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

    if not defer_summarization:
        await start_document_summarization(doc, denial_context=denial_context)
    elif summarization_needed(doc):
        # The watchdog may only claim from the status observed NOW: a fresh
        # PENDING doc must not be re-touched once its fast path has run,
        # while a resubmitted FAILED doc must still get its retry if this
        # turn dies. A re-paste while an earlier watchdog is still parked
        # arms a second one; that is deliberate -- the atomic claim lets at
        # most one dispatch, so extras are harmless no-ops, and deduping the
        # threads themselves would need persisted watchdog state.
        await fire_and_forget_in_new_threadpool(
            _deferred_summarization_watchdog(
                doc.id,
                denial_context,
                DEFERRED_SUMMARY_WATCHDOG_SECONDS,
                claim_statuses=[doc.processing_status],
            )
        )

    return doc

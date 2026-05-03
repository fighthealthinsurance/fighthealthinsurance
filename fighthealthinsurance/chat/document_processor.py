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


async def process_uploaded_document(
    chat,
    document_name: str,
    full_text: str,
    denial_context: Optional[str] = None,
) -> ChatDocument:
    """Create a ChatDocument and fire background summarization.

    Returns the ChatDocument so the caller can reference it.
    """
    doc = await ChatDocument.objects.acreate(
        chat=chat,
        document_name=document_name or "uploaded_document",
        full_text=full_text,
        char_count=len(full_text),
        processing_status=ChatDocument.Status.PENDING,
    )

    logger.info(
        f"Created ChatDocument {doc.id} for chat {chat.id} " f"({len(full_text)} chars)"
    )

    await fire_and_forget_in_new_threadpool(
        summarize_chunks(doc.id, denial_context=denial_context)
    )

    return doc

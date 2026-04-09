"""
Document processing for chat-uploaded files.

Handles chunking large documents and summarizing each chunk using ML models,
so that the full document text doesn't need to sit in the chat history.
"""

import asyncio
import re
from typing import Dict, List, Optional

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router

# Chunk configuration
DEFAULT_CHUNK_SIZE = 4000  # chars per chunk
DEFAULT_OVERLAP = 500  # overlap between chunks
SUMMARIZE_TIMEOUT = 30  # seconds per chunk summarization
OVERALL_SUMMARY_TIMEOUT = 45  # seconds for overall summary


def chunk_document(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[Dict]:
    """
    Split document text into overlapping chunks, breaking at paragraph boundaries.

    Args:
        text: Full document text
        chunk_size: Target characters per chunk
        overlap: Overlap between consecutive chunks

    Returns:
        List of chunk dicts with keys: chunk_index, start_char, end_char, text
    """
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

        # If we're not at the end, try to break at a paragraph or sentence boundary
        if end < len(text):
            # Try paragraph break first (double newline)
            para_break = text.rfind("\n\n", start + chunk_size // 2, end)
            if para_break > start:
                end = para_break + 2  # Include the newlines
            else:
                # Try single newline
                line_break = text.rfind("\n", start + chunk_size // 2, end)
                if line_break > start:
                    end = line_break + 1
                else:
                    # Try sentence end (period followed by space or newline)
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

        # Move start forward, accounting for overlap
        start = max(end - overlap, start + 1)
        if start >= len(text):
            break

    return chunks


async def _summarize_single_chunk(
    chunk_text: str,
    chunk_index: int,
    total_chunks: int,
    denial_context: Optional[str] = None,
) -> Optional[str]:
    """Summarize a single document chunk using internal ML models."""
    context_str = ""
    if denial_context:
        context_str = f"\nContext: This document was uploaded as part of a health insurance denial appeal. {denial_context}\n"

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

    models = ml_router.internal_models_by_cost[:3]

    for model in models:
        try:
            result = await asyncio.wait_for(
                model._infer_no_context(
                    system_prompts=[
                        "You are an expert at analyzing health insurance documents. "
                        "Provide concise, accurate summaries focused on information "
                        "relevant to insurance appeals."
                    ],
                    prompt=prompt,
                    temperature=0.3,
                ),
                timeout=SUMMARIZE_TIMEOUT,
            )
            if result and len(result.strip()) > 20:
                return str(result).strip()
        except asyncio.TimeoutError:
            logger.debug(f"Timeout summarizing chunk {chunk_index} with {model}")
        except Exception as e:
            logger.debug(f"Error summarizing chunk {chunk_index} with {model}: {e}")

    return None


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

    context_str = ""
    if denial_context:
        context_str = f"\nContext: {denial_context}\n"

    prompt = f"""Create a brief overall summary of the document "{document_name}" based on these section summaries.
{context_str}
Section summaries:
{combined[:6000]}

Provide a concise overall summary (max 300 words) that captures the most important information
from this document relevant to a health insurance denial appeal."""

    models = ml_router.internal_models_by_cost[:3]

    for model in models:
        try:
            result = await asyncio.wait_for(
                model._infer_no_context(
                    system_prompts=[
                        "You are an expert at summarizing health insurance documents for appeal purposes."
                    ],
                    prompt=prompt,
                    temperature=0.3,
                ),
                timeout=OVERALL_SUMMARY_TIMEOUT,
            )
            if result and len(result.strip()) > 20:
                return str(result).strip()
        except asyncio.TimeoutError:
            logger.debug(f"Timeout generating overall summary with {model}")
        except Exception as e:
            logger.debug(f"Error generating overall summary with {model}: {e}")

    # Fallback: concatenate chunk summaries
    return f"Document: {document_name}\n\n{combined[:2000]}"


async def summarize_chunks(
    chat_document_id: int,
    denial_context: Optional[str] = None,
) -> None:
    """
    Background task: chunk and summarize a ChatDocument.

    Updates the ChatDocument record progressively as chunks are summarized.
    """
    from fighthealthinsurance.models import ChatDocument

    try:
        doc = await ChatDocument.objects.aget(id=chat_document_id)
    except ChatDocument.DoesNotExist:
        logger.warning(f"ChatDocument {chat_document_id} not found for summarization")
        return

    try:
        doc.processing_status = "processing"
        await doc.asave()

        chunks = chunk_document(doc.full_text)
        if not chunks:
            doc.processing_status = "completed"
            doc.summary = "(Empty document)"
            await doc.asave()
            return

        # Summarize chunks - process in batches of 3 for parallelism
        chunk_results: List[Dict] = []
        batch_size = 3
        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start : batch_start + batch_size]
            tasks = [
                _summarize_single_chunk(
                    chunk["text"],
                    chunk["chunk_index"],
                    len(chunks),
                    denial_context,
                )
                for chunk in batch
            ]
            summaries = await asyncio.gather(*tasks, return_exceptions=True)

            for chunk, summary in zip(batch, summaries):
                chunk_entry = {
                    "chunk_index": chunk["chunk_index"],
                    "start_char": chunk["start_char"],
                    "end_char": chunk["end_char"],
                    "text": chunk["text"],
                    "summary": str(summary) if isinstance(summary, str) else "",
                }
                chunk_results.append(chunk_entry)

            # Save progress incrementally
            doc.chunk_summaries = chunk_results
            await doc.asave()

        # Generate overall summary
        successful_summaries = [c["summary"] for c in chunk_results if c.get("summary")]
        if successful_summaries:
            overall = await _generate_overall_summary(
                successful_summaries, doc.document_name, denial_context
            )
            if overall:
                doc.summary = overall

        doc.processing_status = "completed"
        await doc.asave()
        logger.info(
            f"Completed summarization of ChatDocument {chat_document_id} "
            f"({len(chunks)} chunks, {len(successful_summaries)} summarized)"
        )

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Failed to summarize ChatDocument {chat_document_id}: {e}"
        )
        try:
            doc.processing_status = "failed"
            await doc.asave()
        except Exception:
            pass


async def process_uploaded_document(
    chat,
    document_name: str,
    full_text: str,
    denial_context: Optional[str] = None,
):
    """
    Entry point for processing a document uploaded in chat.

    Creates a ChatDocument record and fires background summarization.
    Returns the ChatDocument so the caller can reference it.
    """
    from fighthealthinsurance.models import ChatDocument
    from fighthealthinsurance.utils import fire_and_forget_in_new_threadpool

    doc = await ChatDocument.objects.acreate(
        chat=chat,
        document_name=document_name or "uploaded_document",
        full_text=full_text,
        char_count=len(full_text),
        processing_status="pending",
    )

    logger.info(
        f"Created ChatDocument {doc.id} for chat {chat.id}: "
        f"{document_name} ({len(full_text)} chars)"
    )

    # Fire background summarization
    await fire_and_forget_in_new_threadpool(
        summarize_chunks(doc.id, denial_context=denial_context)
    )

    return doc

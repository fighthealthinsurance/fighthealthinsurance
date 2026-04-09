"""
Back-searching in large documents uploaded during chat.

When a user asks a question, this module searches previously uploaded documents
to find the most relevant sections and includes them in the LLM context.
"""

import re
from typing import Dict, List, Optional

from loguru import logger

# Configuration
MAX_SEARCH_RESULTS = 5
MAX_CONTEXT_CHARS = 6000
MIN_QUERY_LENGTH = 3


def _extract_search_terms(query: str) -> List[str]:
    """
    Extract meaningful search terms from a user query.

    Splits on whitespace, filters stopwords, and extracts key phrases.
    """
    # Common stopwords to filter
    stopwords = {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "about",
        "what",
        "which",
        "who",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "me",
        "him",
        "us",
        "them",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "and",
        "but",
        "or",
        "if",
        "up",
        "also",
        "tell",
        "say",
        "says",
        "said",
        "please",
        "thank",
        "thanks",
        "hi",
        "hello",
        "document",
        "file",
        "uploaded",
        "page",
    }

    # Split into words and filter
    words = re.findall(r"[a-zA-Z0-9]+(?:[-'][a-zA-Z0-9]+)*", query.lower())
    terms = [w for w in words if w not in stopwords and len(w) >= MIN_QUERY_LENGTH]

    # Also extract quoted phrases if any
    quoted = re.findall(r'"([^"]+)"', query)
    terms.extend(q.lower() for q in quoted if len(q) >= MIN_QUERY_LENGTH)

    return terms


def _score_chunk(chunk_text: str, search_terms: List[str]) -> float:
    """
    Score a chunk by how well it matches the search terms.

    Uses simple term frequency scoring with bonus for exact phrase matches.
    """
    if not search_terms or not chunk_text:
        return 0.0

    text_lower = chunk_text.lower()
    score = 0.0

    for term in search_terms:
        term_lower = term.lower()
        # Count occurrences
        count = text_lower.count(term_lower)
        if count > 0:
            # Diminishing returns for repeated matches
            score += 1.0 + min(count - 1, 4) * 0.25

    # Normalize by number of terms to not favor long queries
    if search_terms:
        score = score / len(search_terms)

    return score


async def search_chat_documents(
    chat_id,
    query: str,
    max_results: int = MAX_SEARCH_RESULTS,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """
    Search all documents for a chat to find sections relevant to the query.

    Args:
        chat_id: The OngoingChat ID (UUID)
        query: The user's message/query
        max_results: Maximum number of chunks to return
        max_chars: Maximum total characters in returned context

    Returns:
        Formatted string with relevant document sections, or empty string
    """
    from fighthealthinsurance.models import ChatDocument

    search_terms = _extract_search_terms(query)
    if not search_terms:
        return ""

    # Get all documents for this chat
    documents = ChatDocument.objects.filter(chat_id=chat_id)
    scored_chunks: List[Dict] = []

    async for doc in documents.aiterator():
        if not doc.chunk_summaries:
            # Document hasn't been chunked yet; search the full text directly
            score = _score_chunk(doc.full_text, search_terms)
            if score > 0:
                # Return a truncated portion of the full text
                scored_chunks.append(
                    {
                        "score": score,
                        "document_name": doc.document_name,
                        "chunk_index": 0,
                        "text": doc.full_text[:max_chars],
                    }
                )
            continue

        for chunk in doc.chunk_summaries:
            chunk_text = chunk.get("text", "")
            if not chunk_text:
                continue
            score = _score_chunk(chunk_text, search_terms)
            if score > 0:
                scored_chunks.append(
                    {
                        "score": score,
                        "document_name": doc.document_name,
                        "chunk_index": chunk.get("chunk_index", 0),
                        "text": chunk_text,
                    }
                )

    if not scored_chunks:
        return ""

    # Sort by score descending
    scored_chunks.sort(key=lambda x: x["score"], reverse=True)

    # Take top results, respecting char limit
    results = []
    total_chars = 0
    for chunk in scored_chunks[:max_results]:
        chunk_text = chunk["text"]
        if total_chars + len(chunk_text) > max_chars:
            # Truncate this chunk to fit
            remaining = max_chars - total_chars
            if remaining < 200:
                break
            chunk_text = chunk_text[:remaining] + "..."

        header = (
            f"[Document: {chunk['document_name']}, Section {chunk['chunk_index'] + 1}]"
        )
        results.append(f"{header}\n{chunk_text}")
        total_chars += len(chunk_text) + len(header)

    return "\n\n---\n\n".join(results)


async def get_document_context_for_llm(
    chat_id,
    user_message: str,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> Optional[str]:
    """
    Get relevant document context to include in LLM calls.

    Checks if the chat has uploaded documents. If so, searches them
    for sections relevant to the user's current message.

    Args:
        chat_id: The OngoingChat ID
        user_message: The user's current message
        max_chars: Maximum characters of document context

    Returns:
        Formatted context string, or None if no relevant content found
    """
    from fighthealthinsurance.models import ChatDocument

    # Quick check: does this chat have any documents?
    has_docs = await ChatDocument.objects.filter(chat_id=chat_id).aexists()
    if not has_docs:
        return None

    result = await search_chat_documents(chat_id, user_message, max_chars=max_chars)
    if not result:
        return None

    return f"Relevant sections from uploaded documents:\n\n{result}"


async def get_document_summaries_for_context(chat_id) -> Optional[str]:
    """
    Get a brief listing of all uploaded documents and their summaries.

    Used to inform the LLM about what documents are available.

    Args:
        chat_id: The OngoingChat ID

    Returns:
        Formatted string listing documents, or None if no documents
    """
    from fighthealthinsurance.models import ChatDocument

    documents = ChatDocument.objects.filter(chat_id=chat_id).order_by("created_at")
    entries = []

    async for doc in documents.aiterator():
        summary_preview = doc.summary[:200] if doc.summary else "(processing...)"
        status = doc.processing_status
        entries.append(
            f"- {doc.document_name} ({doc.char_count:,} chars, {status}): {summary_preview}"
        )

    if not entries:
        return None

    return "Uploaded documents:\n" + "\n".join(entries)

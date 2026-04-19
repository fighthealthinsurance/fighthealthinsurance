"""
Back-searching in large documents uploaded during chat.

When a user asks a question, this module searches previously uploaded documents
to find the most relevant sections and includes them in the LLM context.
"""

import re
from typing import Dict, List, Optional

from fighthealthinsurance.models import ChatDocument

MAX_SEARCH_RESULTS = 5
MAX_CONTEXT_CHARS = 6000
MIN_QUERY_LENGTH = 3

# Words excluded from query term extraction. Includes common English stopwords
# plus conversational filler that isn't meaningful for document search.
_STOPWORDS = frozenset(
    {
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
)

_WORD_RE = re.compile(r"[a-zA-Z0-9]+(?:[-'][a-zA-Z0-9]+)*")
_QUOTED_RE = re.compile(r'"([^"]+)"')


def _extract_search_terms(query: str) -> List[str]:
    """Extract meaningful search terms from a user query."""
    words = _WORD_RE.findall(query.lower())
    terms = [w for w in words if w not in _STOPWORDS and len(w) >= MIN_QUERY_LENGTH]
    terms.extend(
        q.lower() for q in _QUOTED_RE.findall(query) if len(q) >= MIN_QUERY_LENGTH
    )
    return terms


def _score_chunk(chunk_text: str, search_terms: List[str]) -> float:
    """Score a chunk by how well it matches the search terms.

    Uses simple term frequency with diminishing returns for repeated matches,
    normalized by query length so long questions don't artificially win.
    """
    if not search_terms or not chunk_text:
        return 0.0

    text_lower = chunk_text.lower()
    score = 0.0
    for term in search_terms:
        count = text_lower.count(term.lower())
        if count > 0:
            score += 1.0 + min(count - 1, 4) * 0.25

    return score / len(search_terms)


def _format_chunk_text(chunk: Dict, full_text: str) -> str:
    """Return the text for a chunk, reconstructing from offsets if needed."""
    text = chunk.get("text")
    if isinstance(text, str) and text:
        return text
    start: int = chunk.get("start_char", 0)
    end: int = chunk.get("end_char", len(full_text))
    return full_text[start:end]


async def get_document_context_for_message(
    chat_id,
    user_message: str,
    max_chars: int = MAX_CONTEXT_CHARS,
    max_results: int = MAX_SEARCH_RESULTS,
) -> Optional[str]:
    """Build all document context (listing + relevant chunks) in a single pass.

    Returns None if the chat has no uploaded documents.
    """
    if not await ChatDocument.objects.filter(chat_id=chat_id).aexists():
        return None

    search_terms = _extract_search_terms(user_message)

    documents = ChatDocument.objects.filter(chat_id=chat_id).order_by("created_at")

    summary_entries: List[str] = []
    scored_chunks: List[Dict] = []

    async for doc in documents.aiterator():
        summary_preview = (
            doc.summary[:200] if doc.summary else f"({doc.processing_status})"
        )
        summary_entries.append(
            f"- {doc.document_name} ({doc.char_count:,} chars, {doc.processing_status}): {summary_preview}"
        )

        if not search_terms:
            continue

        if not doc.chunk_summaries:
            # Documents still being processed: score and return a preview of
            # the same text so the LLM sees the content we found matches in.
            sample = doc.full_text[:max_chars]
            score = _score_chunk(sample, search_terms)
            if score > 0:
                scored_chunks.append(
                    {
                        "score": score,
                        "document_name": doc.document_name,
                        "chunk_index": 0,
                        "text": sample,
                    }
                )
            continue

        for chunk in doc.chunk_summaries:
            chunk_text = _format_chunk_text(chunk, doc.full_text)
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

    parts: List[str] = []
    if summary_entries:
        parts.append("Uploaded documents:\n" + "\n".join(summary_entries))

    if scored_chunks:
        scored_chunks.sort(key=lambda x: x["score"], reverse=True)
        formatted: List[str] = []
        total_chars = 0
        for chunk in scored_chunks[:max_results]:
            chunk_text = chunk["text"]
            if total_chars + len(chunk_text) > max_chars:
                remaining = max_chars - total_chars
                if remaining < 200:
                    break
                chunk_text = chunk_text[:remaining] + "..."
            header = f"[Document: {chunk['document_name']}, Section {chunk['chunk_index'] + 1}]"
            formatted.append(f"{header}\n{chunk_text}")
            total_chars += len(chunk_text) + len(header)

        if formatted:
            parts.append(
                "Relevant sections from uploaded documents:\n\n"
                + "\n\n---\n\n".join(formatted)
            )

    return "\n\n".join(parts) if parts else ""

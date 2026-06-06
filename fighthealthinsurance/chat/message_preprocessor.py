"""
Message preprocessing for the chat interface.

Turns a raw user message into a list of :class:`MessageVariant` representations.
The *primary* (original) variant is always preferred and scored highest when it
is safe to use. Long-message and weird-Unicode handling are provided only as
*lower-scored alternative* variants so they can win when the primary path fails,
exceeds limits, or would produce an invalid response.

This module is intentionally dependency-free (stdlib only) and must never log
full message contents (PHI).
"""

import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, List, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Size thresholds (characters). These are deliberately char-based, not
# byte-based, so we never slice in the middle of a multi-byte code point.
# ---------------------------------------------------------------------------

# Above this, a message is no longer sent verbatim through normal chat; we
# prefer compact representations and store the original for reference.
DIRECT_CHAT_SOFT_LIMIT_CHARS = 8000

# Absolute ceiling for any variant's ``text_for_llm``. We never fan the full
# raw text of a huge paste out to every backend; variants stay within this.
DIRECT_CHAT_HARD_LIMIT_CHARS = 24000

# Preview sizes used for display markers and head/tail context.
DISPLAY_PREVIEW_CHARS = 2000
TAIL_PREVIEW_CHARS = 500

# Head+tail "summary" variant stays small so the preferred long-message
# alternatives don't push large text through every backend.
LONG_SUMMARY_MAX_CHARS = 6000

# A single whitespace-free run longer than this is suspicious (OCR garbage,
# base64 blobs, etc.). Recorded for observability; display wrapping is handled
# by the frontend CSS.
UNBROKEN_TOKEN_LIMIT_CHARS = 1500

# More than this many consecutive combining marks looks like "Zalgo" text.
COMBINING_MARK_RUN_LIMIT = 8

# ---------------------------------------------------------------------------
# Score deltas. The primary/original path is 0 (highest); everything else is a
# negative alternative. These are added to the per-call base score so a valid
# primary always outranks an alternative for the same backend, while a failed
# primary (scored -inf elsewhere) lets an alternative win.
# ---------------------------------------------------------------------------
PRIMARY_SCORE_DELTA = 0
LONG_DOC_REFERENCE_DELTA = -35
LONG_SUMMARY_DELTA = -70
LONG_TRUNCATED_DELTA = -125
UNICODE_NFC_DELTA = -30
UNICODE_CONTROL_STRIPPED_DELTA = -80
UNICODE_NFKC_DELTA = -120

# Bidi controls, zero-width characters, BOM/word-joiner and the replacement
# character. These are treated as suspicious even though some (ZWJ/ZWNJ) appear
# in legitimate emoji/scripts -- flagging only adds lower-scored alternatives;
# the preserved primary still wins when it is valid. Defined by numeric code
# point so no invisible characters (notably the U+202E "Trojan Source"
# override) live in this source file.
_SUSPICIOUS_CODEPOINTS = (
    0xFFFD,  # REPLACEMENT CHARACTER
    # Bidirectional formatting / overrides / isolates
    0x202A,  # LEFT-TO-RIGHT EMBEDDING
    0x202B,  # RIGHT-TO-LEFT EMBEDDING
    0x202C,  # POP DIRECTIONAL FORMATTING
    0x202D,  # LEFT-TO-RIGHT OVERRIDE
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0x2066,  # LEFT-TO-RIGHT ISOLATE
    0x2067,  # RIGHT-TO-LEFT ISOLATE
    0x2068,  # FIRST STRONG ISOLATE
    0x2069,  # POP DIRECTIONAL ISOLATE
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x061C,  # ARABIC LETTER MARK
    # Zero-width and joiners
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x2060,  # WORD JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
)
_SUSPICIOUS_CHARS = frozenset(chr(cp) for cp in _SUSPICIOUS_CODEPOINTS)

# Joiners we must not leave dangling at the end of a truncation.
_TRAILING_JOINERS = frozenset({chr(0x200D), chr(0x200C)})

# Whitespace we always preserve, even when stripping control characters.
_KEPT_WHITESPACE = "\t\n\r"


@dataclass
class MessageVariant:
    """One representation of a user message to consider sending to the LLM.

    Attributes:
        kind: Stable identifier for the variant (e.g. ``"primary_original"``).
        text_for_llm: The text to send to the model for this variant.
        score_delta: Added to the call's base score; 0 for primary, negative
            for alternatives.
        display_text: What to store/show instead of the original. ``None`` means
            "use the original message as-is" (the common, lossless case).
        metadata: Non-PHI metadata (char counts, document name, previews).
    """

    kind: str
    text_for_llm: str
    score_delta: int
    display_text: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def ensure_encodable(text: str) -> tuple[str, bool]:
    """Guarantee the text can be UTF-8 encoded (JSON/WebSocket/DB safe).

    Python strings may contain lone surrogates (e.g. ``chr(0xD800)``) that crash
    ``str.encode("utf-8")`` -- which is what Django Channels does on send. For
    valid strings this is a no-op and returns the original unchanged, so the
    normal path is byte-identical. Only un-encodable input is sanitized.

    Returns:
        ``(safe_text, was_sanitized)``.
    """
    try:
        text.encode("utf-8")
        return text, False
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8"), True


def longest_unbroken_run(text: str) -> int:
    """Length of the longest run of non-whitespace characters."""
    longest = 0
    current = 0
    for ch in text:
        if ch.isspace():
            current = 0
        else:
            current += 1
            if current > longest:
                longest = current
    return longest


def has_suspicious_unicode(text: str) -> bool:
    """True if the text contains control/bidi/zero-width/replacement chars or an
    excessive run of combining marks. Used only to decide whether to *offer*
    lower-scored normalized alternatives -- the original is still preserved.
    """
    combining_run = 0
    for ch in text:
        if ch in _SUSPICIOUS_CHARS:
            return True
        category = unicodedata.category(ch)
        if category == "Cc" and ch not in _KEPT_WHITESPACE:
            return True
        if unicodedata.combining(ch):
            combining_run += 1
            if combining_run > COMBINING_MARK_RUN_LIMIT:
                return True
        else:
            combining_run = 0
    return False


def strip_control_chars(text: str) -> str:
    """Remove control/format/surrogate/private-use chars (keeping tab/newline)
    and the suspicious set. Used only for lossy fallback variants.
    """
    out: List[str] = []
    for ch in text:
        if ch in _KEPT_WHITESPACE:
            out.append(ch)
            continue
        if ch in _SUSPICIOUS_CHARS:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    return "".join(out)


def safe_display_truncate(text: str, limit: int) -> str:
    """Truncate to at most ``limit`` code points, then append an ellipsis.

    Avoids ending in the middle of a run of combining marks or on a
    zero-width joiner so we don't orphan a sequence. Python strings are code
    points, so this never splits a surrogate pair. Note: this is *not* full
    grapheme-cluster aware (it does not pull in a dependency), so some ZWJ
    emoji sequences may still be split -- but it will not crash.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    # Reserve one code point for the ellipsis so the result length <= limit.
    cut = text[: limit - 1]
    while cut and (unicodedata.combining(cut[-1]) or cut[-1] in _TRAILING_JOINERS):
        cut = cut[:-1]
    return cut + "…"


def _safe_tail(text: str, limit: int) -> str:
    """Last ``limit`` code points, not starting mid combining sequence."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    start = 0
    while start < len(cut) and unicodedata.combining(cut[start]):
        start += 1
    return cut[start:]


def _head_tail_representation(text: str, max_chars: int) -> str:
    """Compact head+tail view bounded by ``max_chars``."""
    if len(text) <= max_chars:
        return text
    elision = "\n\n[... middle content elided ...]\n\n"
    budget = max_chars - len(elision)
    if budget <= 0:
        return safe_display_truncate(text, max_chars)
    head_len = budget // 2
    tail_len = budget - head_len
    head = text[:head_len]
    while head and (unicodedata.combining(head[-1]) or head[-1] in _TRAILING_JOINERS):
        head = head[:-1]
    tail = _safe_tail(text, tail_len)
    return f"{head}{elision}{tail}"


def _build_long_variants(
    safe: str,
    char_count: int,
    document_name: Optional[str],
    max_variant_chars: int,
) -> List[MessageVariant]:
    """Bounded alternatives for an over-soft-limit paste.

    No ``primary_original`` is emitted: the raw text is too large to send
    verbatim or to store in chat history. The full text is preserved by the
    caller (in document storage); these variants stay compact.
    """
    doc_name = document_name or f"pasted_message_{int(time.time())}.txt"
    head = safe_display_truncate(safe, DISPLAY_PREVIEW_CHARS)
    tail = _safe_tail(safe, TAIL_PREVIEW_CHARS)
    marker = (
        f"You pasted a long message (~{char_count:,} chars). "
        f"It has been stored for reference as {doc_name}."
    )
    common_meta: dict[str, Any] = {
        "document_name": doc_name,
        "char_count": char_count,
        "head_preview": head,
        "tail_preview": tail,
        "largest_unbroken_run": longest_unbroken_run(safe),
    }

    reference_text = (
        f"[The user pasted a long message of about {char_count:,} characters. "
        f'The full text has been stored for reference as "{doc_name}" and can be '
        f"searched. Use the beginning and end below to understand the request, and "
        f"ask to search the document for specifics.]\n\n"
        f"Beginning of pasted content:\n{head}\n\n...\n\n"
        f"End of pasted content:\n{tail}"
    )
    cleaned = strip_control_chars(safe)
    return [
        MessageVariant(
            kind="long_message_document_reference",
            text_for_llm=reference_text,
            score_delta=LONG_DOC_REFERENCE_DELTA,
            display_text=marker,
            metadata={**common_meta, "store_full_text": True},
        ),
        MessageVariant(
            kind="long_message_summarized_or_head_tail",
            text_for_llm=_head_tail_representation(
                cleaned, min(max_variant_chars, LONG_SUMMARY_MAX_CHARS)
            ),
            score_delta=LONG_SUMMARY_DELTA,
            display_text=marker,
            metadata=dict(common_meta),
        ),
        MessageVariant(
            kind="long_message_truncated_last_resort",
            text_for_llm=safe_display_truncate(cleaned, max_variant_chars),
            score_delta=LONG_TRUNCATED_DELTA,
            display_text=marker,
            metadata=dict(common_meta),
        ),
    ]


def _build_unicode_variants(
    safe: str, was_sanitized: bool, char_count: int
) -> List[MessageVariant]:
    """Primary (preserved original) plus lower-scored normalized alternatives."""
    nfc = unicodedata.normalize("NFC", safe)
    stripped = strip_control_chars(nfc)
    nfkc = unicodedata.normalize("NFKC", stripped)
    return [
        MessageVariant(
            kind="primary_original",
            text_for_llm=safe,
            score_delta=PRIMARY_SCORE_DELTA,
            # Keep the original in history unless it was unsafe to store/render.
            display_text=None if not was_sanitized else stripped,
            metadata={"char_count": char_count, "suspicious_unicode": True},
        ),
        MessageVariant(
            kind="unicode_nfc",
            text_for_llm=nfc,
            score_delta=UNICODE_NFC_DELTA,
            metadata={"char_count": len(nfc)},
        ),
        MessageVariant(
            kind="unicode_control_stripped",
            text_for_llm=stripped,
            score_delta=UNICODE_CONTROL_STRIPPED_DELTA,
            metadata={"char_count": len(stripped)},
        ),
        MessageVariant(
            kind="unicode_nfkc_search_only_or_last_resort",
            text_for_llm=nfkc,
            score_delta=UNICODE_NFKC_DELTA,
            metadata={"char_count": len(nfkc)},
        ),
    ]


def prepare_user_message_variants(
    user_message: str,
    *,
    is_document: bool,
    document_name: Optional[str] = None,
    max_direct_chars: int = DIRECT_CHAT_SOFT_LIMIT_CHARS,
    max_variant_chars: int = DIRECT_CHAT_HARD_LIMIT_CHARS,
) -> List[MessageVariant]:
    """Build the ordered list of message variants to consider for the LLM.

    The normal case -- a short, clean message -- returns a single
    ``primary_original`` variant with ``text_for_llm == user_message``,
    ``score_delta == 0`` and ``display_text is None``, so downstream behavior is
    identical to sending the raw message.

    Args:
        user_message: The raw user message.
        is_document: Whether this came in via the explicit document-upload path
            (already stored + replaced with a marker upstream).
        document_name: Optional name to use for a stored long paste.
        max_direct_chars: Soft limit; above it we use compact long-message
            variants instead of the raw text.
        max_variant_chars: Hard ceiling for any variant's ``text_for_llm``.
    """
    raw = user_message or ""
    safe, was_sanitized = ensure_encodable(raw)
    char_count = len(safe)

    variants: List[MessageVariant]
    if char_count > max_direct_chars and not is_document:
        variants = _build_long_variants(
            safe, char_count, document_name, max_variant_chars
        )
    elif has_suspicious_unicode(safe):
        variants = _build_unicode_variants(safe, was_sanitized, char_count)
    else:
        variants = [
            MessageVariant(
                kind="primary_original",
                text_for_llm=safe,
                score_delta=PRIMARY_SCORE_DELTA,
                display_text=None if not was_sanitized else safe,
                metadata={"char_count": char_count},
            )
        ]

    # Defensive: never return an empty list.
    if not variants:
        variants = [
            MessageVariant(
                kind="primary_original",
                text_for_llm=safe,
                score_delta=PRIMARY_SCORE_DELTA,
                display_text=None if not was_sanitized else safe,
                metadata={"char_count": char_count},
            )
        ]

    # Log only non-PHI metadata (kinds + deltas + size), never content.
    logger.debug(
        f"prepare_user_message_variants: {len(variants)} variants "
        f"{[(v.kind, v.score_delta) for v in variants]} "
        f"(is_document={is_document}, chars={char_count}, sanitized={was_sanitized})"
    )
    return variants

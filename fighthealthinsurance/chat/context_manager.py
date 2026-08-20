"""
Context management utilities for LLM chat operations.

Handles history truncation, summarization, and context accumulation
to keep chat sessions manageable within LLM context limits.
"""

import asyncio
import uuid
from typing import Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.utils import ensure_message_alternation

# Configuration
DEFAULT_MESSAGES_TO_KEEP = 20
SUMMARIZATION_INTERVAL = 10

MISSING_CONTEXT_PREFIX = "Missing context summary, refer to previous chat history"

# Labels used when combining a fresh history summary with accumulated
# context (see _summarize_history). A previous full-prefix summary is
# REPLACED, not nested: every summarization call covers the entire dropped
# prefix, so the fresh summary supersedes the old block. Without the strip,
# adjacent band triggers (e.g. a failed turn shifting history length from a
# remainder-0 boundary to remainder 1) nested near-identical summaries
# inside each other.
EARLIER_SUMMARY_LABEL = "Earlier conversation summary: "
ADDITIONAL_CONTEXT_SEPARATOR = "\n\nAdditional context: "

# Wrapper labels chat_interface adds when it hands two stored summaries to the
# LLM. They must be recognized here so a stale summary block wrapped in them is
# still stripped (and so a label left dangling by a strip is cleaned up).
_SUMMARY_WRAPPER_LABELS = (
    "Previous context summary:",
    "Most recent context summary:",
)


def strip_previous_summary_blocks(existing_summary: Optional[str]) -> Optional[str]:
    """Remove already-generated summary blocks, keeping other accreted context.

    A fresh summarization covers the ENTIRE dropped prefix, so any earlier
    ``EARLIER_SUMMARY_LABEL`` block it contains is superseded and must be
    dropped rather than nested inside the new one.

    This deliberately searches ANYWHERE in the string rather than testing
    ``startswith``: the production caller never passes a bare summary. It
    passes the model's own per-turn summary, or chat_interface's
    "Previous context summary: ... Most recent context summary: ..." wrapper,
    optionally prefixed by ``TRIMMED_CONTEXT_PREFIX`` -- so a startswith test
    never matched and summaries nested on every band.
    """
    if not existing_summary:
        return None
    index = existing_summary.find(EARLIER_SUMMARY_LABEL)
    if index < 0:
        return existing_summary
    head = existing_summary[:index]
    # Everything from the label onward is the stale summary, except any
    # "Additional context:" tail it carried (which may itself nest more).
    rest = existing_summary[index:].split(ADDITIONAL_CONTEXT_SEPARATOR, 1)
    tail = strip_previous_summary_blocks(rest[1]) if len(rest) > 1 else None
    parts = []
    for part in (head, tail):
        if not part:
            continue
        cleaned = part.strip()
        # Drop wrapper labels left pointing at content we just removed.
        for label in _SUMMARY_WRAPPER_LABELS:
            if cleaned.endswith(label):
                cleaned = cleaned[: -len(label)].strip()
        if cleaned and cleaned not in _SUMMARY_WRAPPER_LABELS:
            parts.append(cleaned)
    return "\n\n".join(parts) or None


def make_placeholder(counter: int) -> str:
    """Create a uniquely-identified placeholder for a missing context summary."""
    return f"{MISSING_CONTEXT_PREFIX} [{counter}]"


# Marker prepended when bound_summary_context trims accreted context.
TRIMMED_CONTEXT_PREFIX = "[...earlier context trimmed...]\n"


def bound_summary_context(context: Optional[str], max_chars: int) -> Optional[str]:
    """Bound an accreted summary-context string to ``max_chars``.

    Summaries accrete over long chats ("Earlier conversation summary: ...
    Additional context: Earlier conversation summary: ..."), and an unbounded
    blob crowds the actual conversation out of the model's attention -- one
    of the ways replies degrade into replaying earlier turns. Keeps the TAIL
    (most recent context lands last in the accreted string) with a marker so
    the model knows earlier context was dropped.
    """
    if not context or max_chars <= 0 or len(context) <= max_chars:
        return context
    keep = max_chars - len(TRIMMED_CONTEXT_PREFIX)
    if keep <= 0:
        # No room for the marker plus any content. `context[-0:]` is the WHOLE
        # string, so returning the marker + slice here produced output LONGER
        # than the input for any max_chars <= len(TRIMMED_CONTEXT_PREFIX) --
        # the bound silently inverted. Hard-truncate instead.
        return context[-max_chars:]
    return f"{TRIMMED_CONTEXT_PREFIX}{context[-keep:]}"


async def prepare_history_for_llm(
    chat_history: Optional[List[Dict[str, str]]],
    existing_summary: Optional[str],
    summarize_callback=None,
) -> Tuple[List[Dict[str, str]], Optional[List[Dict[str, str]]], Optional[str]]:
    """
    Prepare chat history for LLM calls, with optional summarization.

    This function handles:
    1. Keeping full history for models with large context windows
    2. Summarizing older messages to reduce context size
    3. Truncating history to last N messages for the LLM

    Args:
        chat_history: Full chat history from the chat session
        existing_summary: Previously accumulated context summary
        summarize_callback: Optional async callback to notify about summarization

    Returns:
        Tuple of:
            - truncated_history: History truncated to last N messages
            - full_history: Full history (for large context models), or None
            - updated_summary: Updated context summary with any new summarization
    """
    history_for_llm = list(chat_history) if chat_history else []
    # Apply message alternation to input history
    history_for_llm = ensure_message_alternation(history_for_llm)
    full_history_for_llm: Optional[List[Dict[str, str]]] = None
    summarized_context = existing_summary

    if len(history_for_llm) <= DEFAULT_MESSAGES_TO_KEEP:
        # History is short enough, no processing needed
        return history_for_llm, None, summarized_context

    # Preserve full history for models with large context windows
    full_history_for_llm = list(history_for_llm)

    # Check if we should summarize (once per N-message band after the
    # threshold). History normally grows by 2 per turn, but consecutive
    # same-role messages get merged (ensure_message_alternation) so the
    # length can change parity -- a strict `% N == 0` then never fires again
    # and dropped messages stop being folded into the summary at all. `<= 1`
    # fires once per band for either parity. When a +1 length step lands
    # right after a boundary (e.g. a failed turn persisting only its user
    # message), remainders 0 and 1 can BOTH fire in one band; that costs one
    # redundant bounded summarization call, and _summarize_history replaces
    # (never nests) the previous summary block, so the result is identical.
    # Tracking the exact summarized boundary would need persisted state,
    # which this rare case doesn't justify.
    messages_over_threshold = len(history_for_llm) - DEFAULT_MESSAGES_TO_KEEP
    should_summarize = messages_over_threshold % SUMMARIZATION_INTERVAL <= 1

    if should_summarize:
        summarized_context = await _summarize_history(
            history_for_llm[:-DEFAULT_MESSAGES_TO_KEEP],
            summarized_context,
            summarize_callback,
        )

    # Truncate to last N messages for the LLM
    start_idx = max(0, len(history_for_llm) - DEFAULT_MESSAGES_TO_KEEP)
    truncated_history = history_for_llm[start_idx:]
    # ensure_message_alternation will fix any alternation issues from slicing
    truncated_history = ensure_message_alternation(truncated_history)
    logger.debug(f"Truncated history to last {DEFAULT_MESSAGES_TO_KEEP} messages")

    return truncated_history, full_history_for_llm, summarized_context


async def _summarize_history(
    messages_to_summarize: List[Dict[str, str]],
    existing_summary: Optional[str],
    notify_callback=None,
) -> Optional[str]:
    """
    Summarize older messages to reduce context size.

    Args:
        messages_to_summarize: Messages to be summarized
        existing_summary: Existing context summary to append to
        notify_callback: Optional async callback to notify about status

    Returns:
        Updated context summary, or existing summary if summarization failed
    """
    if not messages_to_summarize:
        return existing_summary

    try:
        if notify_callback:
            await notify_callback("Summarizing conversation context...")

        # Hard bound: this runs on the interactive turn path, so a stalled
        # summarization backend must degrade to "keep existing context", not
        # stall the user's reply. (asyncio.TimeoutError lands in the except
        # below.)
        history_summary = await asyncio.wait_for(
            ml_router.summarize_chat_history(
                messages_to_summarize,
                max_messages=0,  # Summarize all dropped messages
            ),
            timeout=90,
        )

        if history_summary:
            # The fresh summary covers the whole dropped prefix, so any
            # previous full-prefix summary block in existing_summary is
            # superseded -- keep only the non-summary context around it
            # (microsite/pubmed additions etc.).
            existing_summary = strip_previous_summary_blocks(existing_summary)
            if existing_summary:
                return (
                    f"{EARLIER_SUMMARY_LABEL}{history_summary}"
                    f"{ADDITIONAL_CONTEXT_SEPARATOR}{existing_summary}"
                )
            return f"{EARLIER_SUMMARY_LABEL}{history_summary}"

        logger.info("Summarization returned empty, keeping existing context")
        return existing_summary

    except Exception as e:
        logger.warning(f"Failed to summarize chat history: {e}")
        return existing_summary


def should_store_summary(
    chat_summary_list: Optional[List[str]],
    new_summary: Optional[str],
) -> bool:
    """
    Determine if a new summary should be stored.

    Args:
        chat_summary_list: Current list of summaries stored in chat
        new_summary: New summary to potentially store

    Returns:
        True if the new summary should be stored
    """
    if not new_summary:
        return False

    if not chat_summary_list:
        return True

    # Store if this is a new/different summary
    return len(chat_summary_list) == 0 or chat_summary_list[-1] != new_summary


def get_current_context(
    chat_summary_list: Optional[List[str]],
    pubmed_context: Optional[str] = None,
) -> Optional[str]:
    """
    Get the current LLM context from stored summaries.

    Args:
        chat_summary_list: List of stored summaries
        pubmed_context: Optional PubMed context to include

    Returns:
        Combined context string, or None if no context available
    """
    current_context = None

    if chat_summary_list and len(chat_summary_list) > 0:
        current_context = chat_summary_list[-1]

    # Add PubMed context if present
    if pubmed_context and current_context:
        current_context = f"{current_context}\n\nPubMed context: {pubmed_context}"
    elif pubmed_context:
        current_context = f"PubMed context: {pubmed_context}"

    return current_context


async def background_generate_summary(chat_id: uuid.UUID, placeholder_tag: str) -> None:
    """
    Generate a context summary in the background when the model omitted the panda emoji.

    Calls ml_router.summarize_chat_history() on the full chat history, then overwrites
    the specific placeholder (identified by placeholder_tag) only if it's still present.
    If a subsequent message has already produced a real summary, the placeholder will have
    been superseded and this is a no-op.
    """
    from fighthealthinsurance.models import OngoingChat

    try:
        chat = await OngoingChat.objects.aget(id=chat_id)
    except OngoingChat.DoesNotExist:
        logger.warning(f"Background summary: chat {chat_id} not found")
        return

    history = chat.chat_history or []
    if not history:
        return

    # Check if the placeholder still exists before doing the expensive ML call.
    # If another message already produced a real summary, the placeholder will
    # have been superseded and we can skip summarization entirely.
    if not chat.summary_for_next_call or not any(
        str(entry) == placeholder_tag for entry in chat.summary_for_next_call
    ):
        logger.debug(
            f"Background summary: placeholder already gone for chat {chat_id}, "
            f"tag={placeholder_tag!r}"
        )
        return

    try:
        summary = await ml_router.summarize_chat_history(history, max_messages=0)
    except Exception as e:
        logger.warning(
            f"Background summary generation failed for chat {chat_id}: "
            f"{type(e).__name__}: {e}"
        )
        return

    if not summary:
        logger.info(f"Background summary generation returned empty for chat {chat_id}")
        return

    # Swap the placeholder for the real summary transactionally against the
    # fresh row (the old refetch-modify-asave here was a lost-update writer
    # racing the main turn's persist).
    from fighthealthinsurance.chat.chat_persistence import (
        areplace_summary_placeholder,
    )

    replaced = await areplace_summary_placeholder(chat_id, placeholder_tag, summary)
    if replaced:
        logger.info(f"Background summary replaced placeholder for chat {chat_id}")
    else:
        logger.debug(
            f"Background summary: placeholder not found in chat {chat_id} "
            f"after re-check, tag={placeholder_tag!r}"
        )

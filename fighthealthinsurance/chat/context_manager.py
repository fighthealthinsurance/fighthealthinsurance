"""
Context management utilities for LLM chat operations.

Handles history truncation, summarization, and context accumulation
to keep chat sessions manageable within LLM context limits.
"""

from typing import Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.utils import ensure_message_alternation


# Configuration
DEFAULT_MESSAGES_TO_KEEP = 20
SUMMARIZATION_INTERVAL = 10


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

    # Check if we should summarize (every N messages after threshold)
    messages_over_threshold = len(history_for_llm) - DEFAULT_MESSAGES_TO_KEEP
    should_summarize = messages_over_threshold % SUMMARIZATION_INTERVAL == 0

    if should_summarize:
        summarized_context = await _summarize_history(
            history_for_llm[:-DEFAULT_MESSAGES_TO_KEEP],
            summarized_context,
            summarize_callback,
        )

    # Truncate to last N messages for the LLM
    truncated_history = history_for_llm[-DEFAULT_MESSAGES_TO_KEEP:]
    # Ensure alternation on truncated history in case slicing broke it
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

        history_summary = await ml_router.summarize_chat_history(
            messages_to_summarize,
            max_messages=0,  # Summarize all dropped messages
        )

        if history_summary:
            if existing_summary:
                return (
                    f"Earlier conversation summary: {history_summary}\n\n"
                    f"Additional context: {existing_summary}"
                )
            return f"Earlier conversation summary: {history_summary}"

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

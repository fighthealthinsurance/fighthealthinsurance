"""
LLM client utilities for chat interface.

Extracts core LLM calling logic from ChatInterface for reusability and testing.
"""

import re
from typing import (
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from loguru import logger

from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.chat.safety_filters import detect_false_promises
from fighthealthinsurance.chat.tools.patterns import ALL_TOOL_PATTERNS as tools_regex


# Response patterns that indicate a bad/leaked response
BAD_RESPONSE_PATTERNS = re.compile(
    r"(The user is a|The assistant is|is helping a patient with their|"
    r"I hope this message finds you well|"
    r"It is a conversation between a patient and an assistant|"
    r"Discussing how to appeal|Helping a patient appeal|the context is|"
    r"The patient was denied coverage for|"
    r"I understand you're seeking assistance with a Semaglutide claim denial appeal|"
    r"The patient is at risk of progression to type 2 diabetes mellitus|"
    r"You are Doughnut|Discussing an appeal for a|My system prompt is)",
    re.IGNORECASE,
)

# Context patterns that indicate a bad context
BAD_CONTEXT_PATTERNS = re.compile(
    r"(^Hi, |my name is doughnut|To help me understand, can you)",
    re.IGNORECASE,
)


def estimate_history_tokens(history: List[Dict[str, str]]) -> int:
    """
    Estimate token count for message history.

    Uses rough approximation of ~4 characters per token.

    Args:
        history: List of message dicts with 'content' keys

    Returns:
        Estimated token count
    """
    return sum(len(msg.get("content", "")) for msg in history) // 4


def score_llm_response(
    result: Optional[Tuple[Optional[str], Optional[str]]],
    call_score: int,
    is_primary_call: bool = True,
) -> float:
    """
    Score an LLM response for quality and safety.

    Args:
        result: Tuple of (response_text, context_part) from LLM
        call_score: Base score from model quality
        is_primary_call: Whether this is a primary (not retry) call

    Returns:
        Float score (higher is better, -inf for invalid responses)
    """
    if result is None:
        return float("-inf")

    response_text, context_part = result

    if not context_part and not response_text:
        return float("-inf")

    score = 0.0

    # Bonus for being a primary call
    if is_primary_call:
        score += 100

    # Score context quality
    if context_part and len(context_part) > 5:
        score += 10
        if BAD_CONTEXT_PATTERNS.search(context_part):
            score -= 5

    # Score response quality
    if response_text and len(response_text) > 5:
        score += 100

        # Penalize responses that leak system prompts
        if BAD_RESPONSE_PATTERNS.search(response_text):
            score -= 75

        # Bonus for tool usage (search anywhere in response)
        for pattern in tools_regex:
            if re.search(pattern, response_text):
                score += 100

        # Safety: Penalize false promises
        if detect_false_promises(response_text):
            score -= 200
            logger.warning("Detected false promise in response, penalizing score")

    # Add base quality score from model
    if response_text and context_part:
        score += call_score
    else:
        score += call_score / 100

    logger.debug(f"Scored response as {score}")
    return score


def create_response_scorer(
    call_scores: Dict[Awaitable, int],
    primary_calls: Optional[List[Awaitable]] = None,
) -> Callable[[Optional[Tuple[Optional[str], Optional[str]]], Awaitable], float]:
    """
    Create a scoring function for use with best_within_timelimit.

    Args:
        call_scores: Dict mapping call awaitables to their base scores
        primary_calls: List of primary (non-retry) calls for bonus scoring

    Returns:
        Scoring function compatible with best_within_timelimit
    """
    primary_set = set(primary_calls) if primary_calls else set()

    def score_fn(
        result: Optional[Tuple[Optional[str], Optional[str]]],
        original_task: Awaitable,
    ) -> float:
        call_score = call_scores.get(original_task, 0)
        is_primary = original_task in primary_set
        return score_llm_response(result, call_score, is_primary)

    return score_fn


def build_llm_calls(
    model_backends: List[RemoteModelLike],
    current_message: str,
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    full_history: Optional[List[Dict[str, str]]] = None,
) -> Tuple[List[Awaitable[Tuple[Optional[str], Optional[str]]]], Dict[Awaitable, int]]:
    """
    Build parallel LLM calls for multiple model backends.

    Args:
        model_backends: List of model backends to call
        current_message: Current message to send
        previous_context_summary: Summary of previous context
        history: Truncated message history
        is_professional: Whether user is a professional
        is_logged_in: Whether user is logged in
        full_history: Full untruncated history (optional)

    Returns:
        Tuple of (list of call awaitables, dict mapping calls to quality scores)
    """
    calls: List[Awaitable[Tuple[Optional[str], Optional[str]]]] = []
    call_scores: Dict[Awaitable, int] = {}

    for model_backend in model_backends:
        # Try with truncated history
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
        )
        calls.append(call)
        call_scores[call] = model_backend.quality() * 20

        # Also try with full history if provided and model can handle it
        if full_history and full_history != history:
            full_history_tokens = estimate_history_tokens(full_history)
            model_max_context = model_backend.get_max_context()
            # Leave room for system prompt and response (~8k tokens)
            available_context = model_max_context - 8000

            if full_history_tokens < available_context:
                full_history_call = model_backend.generate_chat_response(
                    current_message,
                    previous_context_summary=previous_context_summary,
                    history=full_history,
                    is_professional=is_professional,
                    is_logged_in=is_logged_in,
                )
                calls.append(full_history_call)
                # Slightly prefer full history for better context
                call_scores[full_history_call] = model_backend.quality() * 22

    return calls, call_scores


def build_retry_calls(
    model_backends: List[RemoteModelLike],
    current_message: str,
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    fallback_backends: Optional[List[RemoteModelLike]] = None,
) -> Tuple[List[Awaitable[Tuple[Optional[str], Optional[str]]]], Dict[Awaitable, int]]:
    """
    Build retry LLM calls with shortened context and fallback backends.

    Args:
        model_backends: Primary model backends to retry
        current_message: Current message to send
        previous_context_summary: Summary of previous context
        history: Message history (will be shortened if needed)
        is_professional: Whether user is a professional
        is_logged_in: Whether user is logged in
        fallback_backends: Optional backup model backends

    Returns:
        Tuple of (list of call awaitables, dict mapping calls to quality scores)
    """
    calls: List[Awaitable[Tuple[Optional[str], Optional[str]]]] = []
    call_scores: Dict[Awaitable, int] = {}

    # Use shorter history for retry (Python gracefully handles if len < 5)
    retry_history = history[-5:]

    # Try primary backends with shortened history
    for model_backend in model_backends:
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=retry_history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
        )
        calls.append(call)
        call_scores[call] = model_backend.quality() * 15  # Slightly lower score

    # Also try with full history (some models may handle it)
    for model_backend in model_backends:
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
        )
        calls.append(call)
        call_scores[call] = model_backend.quality() * 50

    # Add fallback backends with both short and full histories
    if fallback_backends:
        for model_backend in fallback_backends:
            # Short history for fallbacks (better success rate)
            call = model_backend.generate_chat_response(
                current_message,
                previous_context_summary=previous_context_summary,
                history=retry_history,
                is_professional=is_professional,
                is_logged_in=is_logged_in,
            )
            calls.append(call)
            call_scores[call] = model_backend.quality() * 15

            # Full history for fallbacks (if model can handle it)
            call = model_backend.generate_chat_response(
                current_message,
                previous_context_summary=previous_context_summary,
                history=history,
                is_professional=is_professional,
                is_logged_in=is_logged_in,
            )
            calls.append(call)
            call_scores[call] = model_backend.quality() * 20

    return calls, call_scores

"""
Retry handling utilities for LLM chat operations.

Provides retry logic with fallback strategies for handling LLM failures
gracefully. Uses parallel calls to multiple backends with quality scoring.
"""

from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.chat.llm_client import MIN_RESPONSE_LENGTH, build_retry_calls
from fighthealthinsurance.chat.safety_filters import detect_false_promises
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.utils import best_within_timelimit


def create_simple_retry_scorer(
    call_scores: Dict[Awaitable, int],
) -> Callable[[Optional[Tuple[Optional[str], Optional[str]]], Awaitable], float]:
    """
    Create a simplified scoring function for retry attempts.

    This scorer is less strict than the primary scorer - it focuses on
    getting any valid response rather than the best quality response.

    Args:
        call_scores: Dict mapping call awaitables to their base scores

    Returns:
        Scoring function compatible with best_within_timelimit
    """

    def score_fn(
        result: Optional[Tuple[Optional[str], Optional[str]]],
        original_task: Awaitable,
    ) -> float:
        score = call_scores.get(original_task, 0)

        if result is None:
            return float("-inf")

        response_text, context_part = result

        if not response_text and not context_part:
            return float("-inf")

        # Bonus for having a response
        if response_text and len(response_text) > MIN_RESPONSE_LENGTH:
            score += 100
            # Safety check
            if detect_false_promises(response_text):
                score -= 200

        # Small bonus for context
        if context_part and len(context_part) > MIN_RESPONSE_LENGTH:
            score += 10

        return score

    return score_fn


async def retry_llm_with_fallback(
    model_backends: List[RemoteModelLike],
    current_message: str,
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    fallback_backends: Optional[List[RemoteModelLike]] = None,
    timeout: float = 35.0,
    status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Retry LLM call with shortened context and fallback backends.

    This function implements the retry strategy when primary LLM calls fail:
    1. Try with shortened history (last 5 messages)
    2. Try with full history on primary backends
    3. Try fallback backends if provided

    Args:
        model_backends: Primary model backends to retry
        current_message: Current message to send
        previous_context_summary: Summary of previous context
        history: Full message history
        is_professional: Whether user is a professional
        is_logged_in: Whether user is logged in
        fallback_backends: Optional backup model backends
        timeout: Timeout in seconds for retry attempts
        status_callback: Optional async callback for status messages

    Returns:
        Tuple of (response_text, context_part) or (None, None) on failure
    """
    if status_callback:
        await status_callback("Retrying with optimized context...")

    logger.info("Primary attempt failed, retrying with compacted context")

    # Build retry calls with shortened history and fallbacks
    retry_calls, retry_scores = build_retry_calls(
        model_backends=model_backends,
        current_message=current_message,
        previous_context_summary=previous_context_summary,
        history=history,
        is_professional=is_professional,
        is_logged_in=is_logged_in,
        fallback_backends=fallback_backends,
    )

    # Create simplified scorer for retries
    retry_scorer = create_simple_retry_scorer(retry_scores)

    try:
        retry_response, retry_context = await best_within_timelimit(
            retry_calls,
            retry_scorer,
            timeout=timeout,
        )

        if retry_response and len(retry_response.strip()) > MIN_RESPONSE_LENGTH:
            logger.info("Successfully got response from retry/fallback models")
            return retry_response, retry_context

    except Exception as e:
        logger.warning(f"Fallback models also failed: {e}")

    return None, None


def should_retry_response(
    response_text: Optional[str], min_length: int = MIN_RESPONSE_LENGTH
) -> bool:
    """
    Determine if an LLM response warrants a retry.

    Args:
        response_text: The response text to evaluate
        min_length: Minimum acceptable response length

    Returns:
        True if retry is needed, False if response is acceptable
    """
    if not response_text:
        return True

    if len(response_text.strip()) < min_length:
        return True

    # Retry on safety check failures (false promises)
    if detect_false_promises(response_text):
        logger.warning("Detected false promise in response, triggering retry")
        return True

    return False

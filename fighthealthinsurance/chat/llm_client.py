"""
LLM client utilities for chat interface.

Extracts core LLM calling logic from ChatInterface for reusability and testing.
"""

import re
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.chat.safety_filters import detect_false_promises
from fighthealthinsurance.chat.tools.patterns import ALL_TOOL_PATTERNS as tools_regex
from fighthealthinsurance.ml.ml_models import RemoteModelLike, repetition_penalty
from fighthealthinsurance.utils import ensure_message_alternation

# Response patterns that indicate a bad/leaked response
BAD_RESPONSE_PATTERNS = re.compile(
    r"(The user is a|The assistant is|is helping a patient with their|"
    r"I hope this message finds you well|"
    r"It is a conversation between a patient and an assistant|"
    r"Discussing how to appeal|Helping a patient appeal|the context is|"
    r"The patient was denied coverage for|"
    r"The patient is at risk of progression to type 2 diabetes mellitus|"
    r"You are Doughnut|Discussing an appeal for a|My system prompt is)",
    re.IGNORECASE,
)

# Context patterns that indicate a bad context
BAD_CONTEXT_PATTERNS = re.compile(
    r"(^Hi, |my name is doughnut|To help me understand, can you)",
    re.IGNORECASE,
)

# Minimum length for a valid LLM response or context
MIN_RESPONSE_LENGTH = 5

# Tokens reserved for system prompt, context summary, and response generation
RESERVED_CONTEXT_TOKENS = 8000

# Pattern to detect when the response asks for the patient's name
ASKS_FOR_PATIENT_NAME = re.compile(
    r"(what is (?:the |your )?patient'?s? name|"
    r"(?:could|can|would) you (?:please )?(?:provide|tell|give|share) (?:me )?(?:the |your )?patient'?s? name|"
    r"(?:what|who) is the (?:patient|name of the patient)|"
    r"I(?:'ll| will) need (?:the |your )?patient'?s? name)",
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


# Common file extensions to detect document names in chat history
_FILE_EXT_PATTERN = re.compile(
    r"[\w\-. ]+\.(?:pdf|jpg|jpeg|png|gif|docx|doc|txt|csv|xlsx|xls|rtf|tiff?|bmp|webp)\b",
    re.IGNORECASE,
)


def _extract_document_names_from_history(
    chat_history: List[Dict[str, str]],
) -> List[str]:
    """Extract document filenames from recent chat history messages."""
    names: List[str] = []
    for msg in chat_history[-10:]:  # Only check recent messages
        content = msg.get("content", "")
        for match in _FILE_EXT_PATTERN.finditer(content):
            names.append(match.group(0).strip())
    return names


# --- Repetition penalty helpers ---

# Penalty for response that exactly matches (normalized) the last user/assistant message
EXACT_REPEAT_PENALTY = -500.0
# Penalty for response with same bag-of-words as the last user/assistant message
BAG_OF_WORDS_REPEAT_PENALTY = -75.0
# Lighter penalties for matching older messages in the history
OLDER_ASSISTANT_REPEAT_PENALTY = -20.0
OLDER_USER_REPEAT_PENALTY = -10.0


def normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip."""
    return re.sub(r"\s+", " ", text.lower().strip())


def bag_of_words(text: str) -> set:
    """Extract a bag of words (lowercased) from text for unordered comparison."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def compute_repetition_penalty(
    response_text: str,
    chat_history: List[Dict[str, str]],
    current_message: Optional[str] = None,
) -> float:
    """
    Compute a penalty for responses that repeat previous messages.

    Greatly penalizes exact matches (ignoring case/whitespace) with the
    current user message or the last assistant message.  Mildly penalizes
    same bag-of-words in different order.  Applies lighter penalties for
    matching older messages.

    Args:
        response_text: The candidate response text to evaluate.
        chat_history: The chat history *before* the current turn.
        current_message: The user message that triggered this response.
            Because scoring runs before the message is appended to
            chat_history, this must be supplied separately.
    """
    if not response_text:
        return 0.0

    if not chat_history and not current_message:
        return 0.0

    penalty = 0.0
    normalized_response = normalize_text(response_text)
    response_bow = bag_of_words(response_text)

    # --- Check against the current user message (not yet in history) ---
    if current_message:
        normalized_current = normalize_text(current_message)
        if normalized_response == normalized_current:
            penalty += EXACT_REPEAT_PENALTY
            logger.debug(
                "Response is exact repeat of current user message, applying heavy penalty"
            )
        elif response_bow == bag_of_words(current_message):
            penalty += BAG_OF_WORDS_REPEAT_PENALTY
            logger.debug(
                "Response has same bag-of-words as current user message, applying mild penalty"
            )

    if not chat_history:
        return penalty

    # Find the last user and assistant messages from history
    last_user_msg: Optional[str] = None
    last_assistant_msg: Optional[str] = None
    for msg in reversed(chat_history):
        if msg.get("role") == "user" and last_user_msg is None:
            last_user_msg = msg.get("content", "")
        elif msg.get("role") == "assistant" and last_assistant_msg is None:
            last_assistant_msg = msg.get("content", "")
        if last_user_msg is not None and last_assistant_msg is not None:
            break

    # Heavy penalties for matching the immediate previous messages in history
    for prev_text in [last_user_msg, last_assistant_msg]:
        if not prev_text:
            continue
        normalized_prev = normalize_text(prev_text)
        if normalized_response == normalized_prev:
            penalty += EXACT_REPEAT_PENALTY
            logger.debug(
                "Response is exact repeat of previous message, applying heavy penalty"
            )
        elif response_bow == bag_of_words(prev_text):
            penalty += BAG_OF_WORDS_REPEAT_PENALTY
            logger.debug(
                "Response has same bag-of-words as previous message, applying mild penalty"
            )

    # Lighter penalties for matching older messages
    for msg in chat_history:
        content = msg.get("content", "")
        if not content:
            continue
        # Skip the immediate messages we already checked
        if content == last_user_msg or content == last_assistant_msg:
            continue
        normalized_prev = normalize_text(content)
        if normalized_response == normalized_prev:
            if msg.get("role") == "assistant":
                penalty += OLDER_ASSISTANT_REPEAT_PENALTY
                logger.debug(
                    "Response repeats an older assistant message, applying -20 penalty"
                )
            else:
                penalty += OLDER_USER_REPEAT_PENALTY
                logger.debug(
                    "Response repeats an older user message, applying -10 penalty"
                )

    return penalty


def score_llm_response(
    result: Optional[Tuple[Optional[str], Optional[str]]],
    call_score: int,
    is_primary_call: bool = True,
    chat_history: Optional[List[Dict[str, str]]] = None,
    current_message: Optional[str] = None,
) -> float:
    """
    Score an LLM response for quality and safety.

    Args:
        result: Tuple of (response_text, context_part) from LLM
        call_score: Base score from model quality
        is_primary_call: Whether this is a primary (not retry) call
        chat_history: Current chat history for context-aware scoring
        current_message: The user message that triggered this response.
            Passed separately because it is not yet in chat_history
            at scoring time.

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
    if context_part and len(context_part) > MIN_RESPONSE_LENGTH:
        score += 10
        if BAD_CONTEXT_PATTERNS.search(context_part):
            score -= 5

    # Score response quality
    if response_text and len(response_text) > MIN_RESPONSE_LENGTH:
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

        # Bonus for referencing an uploaded document by name (derived from history)
        if chat_history:
            response_lower = response_text.lower()
            for doc_name in _extract_document_names_from_history(chat_history):
                if doc_name.lower() in response_lower:
                    score += 150
                    logger.debug(
                        f"Response references uploaded document '{doc_name}', boosting score"
                    )
                    break

        # Penalize repetitive responses so cleaner alternatives are preferred
        rep_penalty = repetition_penalty(response_text) * 200
        if rep_penalty > 0:
            score -= rep_penalty
            logger.debug(f"Repetition penalty: -{rep_penalty:.0f}")

        # Always penalize asking for patient name — it's known client-side
        if ASKS_FOR_PATIENT_NAME.search(response_text):
            score -= 200
            logger.debug(
                "Response asks for patient name, penalizing (known client-side)"
            )

        # Penalize responses that repeat previous messages
        if chat_history or current_message:
            score += compute_repetition_penalty(
                response_text, chat_history or [], current_message
            )

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
    chat_history: Optional[List[Dict[str, str]]] = None,
    current_message: Optional[str] = None,
) -> Callable[[Optional[Tuple[Optional[str], Optional[str]]], Awaitable], float]:
    """
    Create a scoring function for use with best_within_timelimit.

    Args:
        call_scores: Dict mapping call awaitables to their base scores
        primary_calls: List of primary (non-retry) calls for bonus scoring
        chat_history: Current chat history for context-aware scoring
        current_message: The user message that triggered this response.
            Passed separately because it is not yet in chat_history
            at scoring time.

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
        return score_llm_response(
            result,
            call_score,
            is_primary,
            chat_history=chat_history,
            current_message=current_message,
        )

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

    # Pre-compute full history token count once (used for context-window checks)
    full_history_tokens = (
        estimate_history_tokens(full_history)
        if full_history and full_history != history
        else None
    )

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
        # Quadratic scaling to more aggressively prefer higher-quality models
        call_scores[call] = (model_backend.quality() ** 2) // 5

        # Also try with full history if provided and model can handle it
        if full_history_tokens is not None:
            available_context = (
                model_backend.get_max_context() - RESERVED_CONTEXT_TOKENS
            )

            if full_history_tokens < available_context:
                full_history_call = model_backend.generate_chat_response(
                    current_message,
                    previous_context_summary=previous_context_summary,
                    history=full_history,
                    is_professional=is_professional,
                    is_logged_in=is_logged_in,
                )
                calls.append(full_history_call)
                # Prefer full history for better context
                call_scores[full_history_call] = (model_backend.quality() ** 2) // 4

    return calls, call_scores


def build_retry_calls(
    model_backends: List[RemoteModelLike],
    current_message: str,
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    fallback_backends: Optional[List[RemoteModelLike]] = None,
    full_history: Optional[List[Dict[str, str]]] = None,
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
        full_history: Full untruncated history (optional)

    Returns:
        Tuple of (list of call awaitables, dict mapping calls to quality scores)
    """
    calls: List[Awaitable[Tuple[Optional[str], Optional[str]]]] = []
    call_scores: Dict[Awaitable, int] = {}

    # Use shorter history for retry (Python gracefully handles if len < 5)
    start_idx = max(0, len(history) - 5)
    retry_history = ensure_message_alternation(history[start_idx:])

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
        # Quadratic scaling, reduced for retry (lower priority than primary)
        call_scores[call] = (model_backend.quality() ** 2) // 7

    # Also try with truncated history (some models may handle it)
    for model_backend in model_backends:
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
        )
        calls.append(call)
        call_scores[call] = (model_backend.quality() ** 2) // 2

    # Pre-compute full history token count once (used for context-window checks)
    full_history_tokens = (
        estimate_history_tokens(full_history)
        if full_history and full_history != history
        else None
    )

    # Try with actual full history if available and different from truncated
    if full_history_tokens is not None:
        for model_backend in model_backends:
            available_context = (
                model_backend.get_max_context() - RESERVED_CONTEXT_TOKENS
            )

            if full_history_tokens < available_context:
                full_call = model_backend.generate_chat_response(
                    current_message,
                    previous_context_summary=previous_context_summary,
                    history=full_history,
                    is_professional=is_professional,
                    is_logged_in=is_logged_in,
                )
                calls.append(full_call)
                # Prefer full history but keep gap small enough that
                # a false-promise penalty (-200) can still override it
                call_scores[full_call] = (model_backend.quality() ** 2) // 2 + 100

    # Add fallback backends with short, truncated, and full histories
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
            call_scores[call] = (model_backend.quality() ** 2) // 7

            # Truncated history for fallbacks
            call = model_backend.generate_chat_response(
                current_message,
                previous_context_summary=previous_context_summary,
                history=history,
                is_professional=is_professional,
                is_logged_in=is_logged_in,
            )
            calls.append(call)
            call_scores[call] = (model_backend.quality() ** 2) // 5

            # Full history for fallbacks (if available and model can handle it)
            if full_history_tokens is not None:
                available_context = (
                    model_backend.get_max_context() - RESERVED_CONTEXT_TOKENS
                )

                if full_history_tokens < available_context:
                    full_call = model_backend.generate_chat_response(
                        current_message,
                        previous_context_summary=previous_context_summary,
                        history=full_history,
                        is_professional=is_professional,
                        is_logged_in=is_logged_in,
                    )
                    calls.append(full_call)
                    # Prefer full history for fallbacks too
                    call_scores[full_call] = (model_backend.quality() ** 2) // 5 + 100

    return calls, call_scores

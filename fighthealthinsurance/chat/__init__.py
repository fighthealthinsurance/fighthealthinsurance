"""
Chat package - contains chat interface modules and tool handlers.
"""
from .safety_filters import (
    detect_crisis_keywords,
    detect_false_promises,
    CRISIS_RESOURCES,
)
from .llm_client import (
    estimate_history_tokens,
    score_llm_response,
    create_response_scorer,
    build_llm_calls,
    build_retry_calls,
    BAD_RESPONSE_PATTERNS,
    BAD_CONTEXT_PATTERNS,
)

__all__ = [
    # Safety filters
    "detect_crisis_keywords",
    "detect_false_promises",
    "CRISIS_RESOURCES",
    # LLM client utilities
    "estimate_history_tokens",
    "score_llm_response",
    "create_response_scorer",
    "build_llm_calls",
    "build_retry_calls",
    "BAD_RESPONSE_PATTERNS",
    "BAD_CONTEXT_PATTERNS",
]

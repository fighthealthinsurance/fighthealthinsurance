"""
Chat package - contains chat interface modules and tool handlers.
"""

from .llm_client import (
    BAD_CONTEXT_PATTERNS,
    BAD_RESPONSE_PATTERNS,
    build_llm_calls,
    build_retry_calls,
    create_response_scorer,
    estimate_history_tokens,
    score_llm_response,
)
from .safety_filters import (
    CRISIS_RESOURCES,
    detect_crisis_keywords,
    detect_false_promises,
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

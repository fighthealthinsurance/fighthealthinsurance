"""Shared heuristics for rejecting unusable LLM output."""

import re
from typing import Callable, Optional

# Canonical guardrail phrases that indicate unusable/refusal-style LLM output.
BAD_OUTPUT_PHRASES: tuple[str, ...] = (
    "Therefore, the Health Plans denial should be overturned.",
    "llama llama virus",
    "The independent medical review found that",
    "The independent review findings for",
    "the physician reviewer overturned",
    "91111111111111111111111",
    "I need the text to be able to help you with your appeal",
    "I cannot directly create",
    "As an AI, I do not have the capability",
    "Unfortunately, I cannot directly",
    "I am an AI assistant and do not have the authority to create medical documents",
    "The Newly Elected Government is Exploiting the Economic Downturn",
    "The Government's Spending Policies Are Exacerbating the Downturn",
    "The Government Needs to Increase Its Spending to Stimulate the Economy",
)

BAD_OUTPUT_PHRASES_LOWER: tuple[str, ...] = tuple(
    phrase.lower() for phrase in BAD_OUTPUT_PHRASES
)

# Boilerplate denial-letter phrases that get incorrectly extracted as
# the procedure/service name.  Used as case-insensitive substring matches.
BOILERPLATE_SERVICE_PHRASES: tuple[str, ...] = (
    "above referenced service",
    "not medically necessary",
    "amount billed",
    "amount not covered",
    "this means the service",
    "information submitted and your plan documents",
    "erisa",
    "requested services are not covered",
    "if your appeal",
    "health care provider can bill",
    "this review indicated",
    "this service is not",
)


# Pre-compiled pattern that matches any boilerplate phrase (case-insensitive).
_BOILERPLATE_RE = re.compile(
    "|".join(re.escape(p) for p in BOILERPLATE_SERVICE_PHRASES),
    re.IGNORECASE,
)

# Separators commonly found between the real value and boilerplate
# (e.g. "MRI scan - not medically necessary", "Osteoarthritis; amount not covered").
_LEADING_SEP_RE = re.compile(r"^[\s\-;:,]+")
_TRAILING_SEP_RE = re.compile(r"[\s\-;:,]+$")


def strip_boilerplate_service(val: str) -> Optional[str]:
    """Remove boilerplate denial phrases from *val* and return the useful remainder.

    Returns ``None`` if nothing meaningful (len < 3) is left after stripping.
    """
    cleaned = _BOILERPLATE_RE.sub("", val)
    # Remove leading/trailing separators left behind (e.g. " - " after phrase removal).
    cleaned = _LEADING_SEP_RE.sub("", cleaned)
    cleaned = _TRAILING_SEP_RE.sub("", cleaned).strip()
    if len(cleaned) < 3:
        return None
    return cleaned


def is_bad_output(
    result: Optional[str],
    *,
    min_length: int = 3,
    check_guardrail_phrases: bool = False,
    check_severe_repetition: bool = False,
    repetition_checker: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Return ``True`` when LLM output should be treated as unusable."""
    if result is None:
        return True

    if len(result.strip()) < min_length:
        return True

    result_lower = result.lower()
    if check_guardrail_phrases and any(
        phrase in result_lower for phrase in BAD_OUTPUT_PHRASES_LOWER
    ):
        return True

    if (
        check_severe_repetition
        and repetition_checker is not None
        and repetition_checker(result)
    ):
        return True

    return False

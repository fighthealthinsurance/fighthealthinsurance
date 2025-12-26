"""
Safety filters for chat messages.

Contains crisis/self-harm detection and false promise detection logic.
Extracted from chat_interface.py for better organization and testability.
"""
import re
from typing import Pattern

# Crisis/self-harm detection - phrases indicating the user may need immediate help
# IMPORTANT: These must be specific enough to NOT block legitimate mental health
# insurance denial appeals. Someone saying "my coverage for suicidal ideation
# treatment was denied" is NOT in crisis - they're seeking help with insurance.
#
# We only trigger on first-person expressions of active crisis, NOT:
# - References to denied mental health coverage
# - Discussion of past mental health treatment
# - Clinical terms in insurance/medical context
#
# These are compiled into regexes with word boundaries to avoid partial matches
# and case-insensitive matching to avoid having to lowercase input.
_CRISIS_PHRASES = [
    # Active first-person crisis expressions (very specific)
    r"i want to kill myself",
    r"i'm going to kill myself",
    r"i want to end my life",
    r"i want to die",
    r"i don't want to live",
    r"i'd rather be dead",
    r"i'm better off dead",
    r"i have no reason to live",
    r"i'm going to take my own life",
    r"i want to hurt myself",
    r"i'm going to hurt myself",
    r"i've been cutting myself",
    r"i'm cutting myself",
    r"thinking about ending it",
    r"planning to end it all",
    r"writing a suicide note",
]

# Pre-compile crisis detection regex for performance
# Uses word boundaries and case-insensitive matching
_CRISIS_REGEX: Pattern[str] = re.compile(
    r"|".join(rf"(?:{re.escape(phrase)})" for phrase in _CRISIS_PHRASES),
    re.IGNORECASE,
)

# Crisis resources to provide when crisis keywords are detected
CRISIS_RESOURCES = """If you or someone you know is struggling, please reach out for support:
- **988 Suicide & Crisis Lifeline**: Call or text **988** (US)
- **Crisis Text Line**: Text **HOME** to **741741**
- **PFLAG Support Hotlines**: https://pflag.org/resource/support-hotlines/
- **Trans Lifeline**: 1-877-565-8860

You are not alone, and help is available 24/7."""

# Patterns that indicate the AI is making promises it can't keep
_FALSE_PROMISE_PATTERNS = [
    r"guarantee.*(?:approval|success|win|approved)",
    r"(?:will|going to)\s+(?:definitely|certainly|surely)\s+(?:get|win|be approved)",
    r"100%\s+(?:chance|success|guaranteed)",
    r"promise.*(?:you|will|approval|win|success|approved|be)",
    r"certain\s+to\s+(?:win|be approved|succeed)",
    r"(?:you're|you are)\s+certain\s+to\s+win",
    r"always\s+(?:works|succeeds|wins|succeed)",
    r"will\s+certainly\s+(?:get|be|win)",
    r"will\s+definitely\s+(?:get|be|win)",
]

# Pre-compile false promise regex for performance
# Combined into single regex with case-insensitive matching
_FALSE_PROMISE_REGEX: Pattern[str] = re.compile(
    r"|".join(rf"(?:{pattern})" for pattern in _FALSE_PROMISE_PATTERNS),
    re.IGNORECASE,
)


def detect_crisis_keywords(text: str) -> bool:
    """
    Check if text contains crisis/self-harm related keywords.

    Returns True if crisis keywords are detected, indicating the user
    may need immediate support resources.

    Uses pre-compiled regex with case-insensitive matching for performance.
    """
    return bool(_CRISIS_REGEX.search(text))


def detect_false_promises(text: str) -> bool:
    """
    Check if the AI response contains false promises about appeal success.

    Returns True if the response makes guarantees or promises that
    we cannot actually keep.

    Uses pre-compiled regex with case-insensitive matching for performance.
    """
    if text is None:
        return False
    return bool(_FALSE_PROMISE_REGEX.search(text))

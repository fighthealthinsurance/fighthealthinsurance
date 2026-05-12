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
    r"|".join(rf"(?:\b{re.escape(phrase)}\b)" for phrase in _CRISIS_PHRASES),
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

# Delete-data request detection - phrases where the user is asking us to
# delete their account or data. We do NOT delete data from chat; instead we
# point them at the self-service flow at /remove_data which verifies email
# ownership before deleting anything.
#
# Patterns are narrow enough to avoid matching things like "delete this
# paragraph from my appeal" or "remove the diagnosis from the record".
_DELETE_DATA_PHRASES = [
    r"delete my (?:data|account|info(?:rmation)?|profile|records?)",
    r"remove my (?:data|account|info(?:rmation)?|profile|records?)",
    r"erase my (?:data|account|info(?:rmation)?|profile|records?)",
    r"wipe my (?:data|account|info(?:rmation)?|profile|records?)",
    r"close my account",
    r"cancel my account",
    r"deactivate my account",
    r"delete everything (?:about|on) me",
    r"forget (?:me|my data|my account)",
    r"gdpr (?:delete|deletion|erasure|request)",
    r"right to (?:be forgotten|erasure)",
    r"opt out of (?:data|having my data)",
]

_DELETE_DATA_REGEX: Pattern[str] = re.compile(
    r"|".join(rf"(?:\b{phrase}\b)" for phrase in _DELETE_DATA_PHRASES),
    re.IGNORECASE,
)

# Sentinel the LLM can emit (per system-prompt instructions) to hand off to
# the canned response when the user's phrasing is too oblique for the regex.
DELETE_DATA_SENTINEL = "[[DELETE_DATA_REQUEST]]"
_DELETE_DATA_SENTINEL_REGEX: Pattern[str] = re.compile(
    re.escape(DELETE_DATA_SENTINEL), re.IGNORECASE
)

DELETE_DATA_RESPONSE = """It looks like you're asking us to delete your data. I can't do that from chat, but you can request deletion yourself:

**[Go to the Remove My Data page](/remove_data)**

Enter your email there and we'll send you a confirmation link. After you click it, your data will be deleted. This two-step flow exists so we can verify you actually own the email before removing anything.

If you have other questions about your appeal or denial, I'm happy to keep helping."""


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


def detect_delete_data_request(text: str) -> bool:
    """
    Check if a user message is asking us to delete their data or account.

    Returns True when the message matches a known data-deletion phrasing.
    Used to short-circuit the chat and direct the user to the self-service
    /remove_data flow instead of asking the LLM to handle it.
    """
    if not text:
        return False
    return bool(_DELETE_DATA_REGEX.search(text))


def llm_requested_delete_handoff(text: str) -> bool:
    """
    Check if an LLM response contains the delete-data sentinel token.

    The system prompt instructs the model to emit this sentinel when it
    recognizes a deletion request the regex missed. We swap the entire
    response for the canned text when the sentinel is present.
    """
    if not text:
        return False
    return bool(_DELETE_DATA_SENTINEL_REGEX.search(text))

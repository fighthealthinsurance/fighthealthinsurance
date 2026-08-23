"""
Safety filters for chat messages.

Contains crisis/self-harm detection and false promise detection logic.
Extracted from chat_interface.py for better organization and testability.
"""

import re
from typing import Optional, Pattern

from fighthealthinsurance.ml.medicaid_names import MEDICAID_PROGRAM_ALIASES

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


def detect_delete_data_request(text: Optional[str]) -> bool:
    """
    Check if a user message is asking us to delete their data or account.

    Returns True when the message matches a known data-deletion phrasing.
    Used to short-circuit the chat and direct the user to the self-service
    /remove_data flow instead of asking the LLM to handle it.
    """
    if not text:
        return False
    return bool(_DELETE_DATA_REGEX.search(text))


def llm_requested_delete_handoff(text: Optional[str]) -> bool:
    """
    Check if an LLM response contains the delete-data sentinel token.

    The system prompt instructs the model to emit this sentinel when it
    recognizes a deletion request the regex missed. We swap the entire
    response for the canned text when the sentinel is present.

    Accepts Optional[str] because the LLM-output cleaning helpers
    (remove_repeated_blocks / remove_repeated_sentences) can return None.
    """
    if not text:
        return False
    return bool(_DELETE_DATA_SENTINEL_REGEX.search(text))


# Program-eligibility verdicts the model must NOT state on its own. Only the
# medicaid_eligibility checker can produce one: it applies the FPL tables, the
# MAGI/ABD/LTC branches and the work-requirement overlay. A model asserting
# "you are eligible for Medicaid" from the conversation alone is guessing
# about someone's health coverage, and in the observed fan-out that guess beat
# the candidate that actually called the checker.
#
# These deliberately match only DEFINITE verdicts. Hedged, invitational
# phrasing ("you may be eligible -- want me to check?") is exactly what the
# Medicaid path is supposed to say before the checker has run, so it must not
# be caught; _ELIGIBILITY_HEDGE_REGEX below clears those sentences.
# The programs a verdict can be about. State names are in here because the
# system prompt tells the model to use whichever name the user does, so
# "you qualify for Medi-Cal" is the same claim as "you qualify for Medicaid".
# Longest-first so the alternation prefers "Medical Assistance Program" over
# its own prefix.
# Aliases that are ordinary English before they are program names. They stay
# in MEDICAID_PROGRAM_ALIASES -- the prompt wants them, because a user saying
# "STAR" tells the model they are in Texas -- but they are NOT evidence that a
# sentence is handing down an eligibility verdict. "You qualify for medical
# assistance benefits" and "you qualify for STAR" were both being read as
# invented Medicaid verdicts, and a false positive here now costs a candidate
# its whole model prior, so the detector errs toward missing a verdict about a
# generically-named program rather than penalizing a legitimate reply.
_AMBIGUOUS_FOR_VERDICTS = frozenset(
    {"STAR", "Medical Assistance", "Medical Assistance Program"}
)

_PROGRAM_ALTERNATION = "|".join(
    re.escape(name)
    for name in sorted(
        ("Medicaid", "Medicare", "CHIP", "Medicaid expansion")
        + tuple(
            name
            for name in MEDICAID_PROGRAM_ALIASES
            if name not in _AMBIGUOUS_FOR_VERDICTS
        ),
        key=len,
        reverse=True,
    )
)

_ELIGIBILITY_VERDICT_PATTERNS = [
    # "you are eligible for Medicaid", "you're not eligible for Medicare",
    # "you are likely ineligible for CHIP"
    r"\byou(?:'re|\s+are|\s+would\s+be|\s+appear\s+to\s+be|\s+seem\s+to\s+be)"
    r"(?:\s+(?:not|likely|probably|clearly|definitely|indeed|already))*"
    rf"\s+(?:in)?eligible\s+for\s+(?:\w+[-\s]+){{0,3}}?(?:{_PROGRAM_ALTERNATION})\b",
    # "you qualify for Medicaid", "you do not qualify for Medicare"
    r"\byou(?:\s+(?:do|does|will|would|should|clearly|likely|probably|"
    r"definitely|already|do\s+not|don't|won't|wouldn't))*"
    rf"\s+qualif(?:y|ies)\s+for\s+(?:\w+[-\s]+){{0,3}}?(?:{_PROGRAM_ALTERNATION})\b",
    # "you are Medicaid-eligible", "your household is not MassHealth eligible"
    r"\b(?:you|your\s+household)\s+(?:is|are)\s+(?:not\s+)?(?:currently\s+)?"
    rf"(?:{_PROGRAM_ALTERNATION})[- ]eligible\b",
    # "you will be approved for Medicaid" -- an approval the model is
    # predicting rather than one the checker (or an agency) decided.
    #
    # Only the FORWARD-LOOKING forms are here. Present and past tense
    # ("you are approved for Medi-Cal", "you were denied Medicaid coverage")
    # are usually the model repeating something the USER told it -- this is an
    # insurance-DENIAL product, so a user's own denial is the normal subject
    # of the conversation, not a hallucination -- and penalizing that would
    # cost a correct reply its model prior.
    r"\byou(?:'ll|\s+will|\s+would|\s+are\s+going\s+to)"
    r"(?:\s+(?:not|likely|probably|clearly|definitely))*"
    r"\s+be\s+(?:approved\s+for|denied(?:\s+for)?)\s+"
    rf"(?:\w+[-\s]+){{0,3}}?(?:{_PROGRAM_ALTERNATION})\b",
]

_ELIGIBILITY_VERDICT_REGEX: Pattern[str] = re.compile(
    r"|".join(rf"(?:{pattern})" for pattern in _ELIGIBILITY_VERDICT_PATTERNS),
    re.IGNORECASE,
)

# Wording that turns the verdict back into a possibility, a condition, or an
# offer to run the real check -- but ONLY where it actually scopes the claim.
# Position matters, and ignoring it broke the detector in both directions:
#
#   "You qualify for Medicaid, but check your state's website to confirm."
#   "You are eligible for Medicaid, though you may need documentation."
#
# were both read as hedged and let through, even though the system prompt
# REQUIRES those caveats on every eligibility answer -- so in practice almost
# no invented verdict was ever caught. Meanwhile
#
#   "You are eligible for Medicaid if your income is under 138% FPL."
#
# was flagged as a definite verdict, because `if\s+you\b` cannot match "if
# your" -- and that is the phrasing the prompt asks for on "what are the
# income limits?".
_ELIGIBILITY_HEDGE_REGEX: Pattern[str] = re.compile(
    r"\b(?:may|might|could|maybe|perhaps|possibly|potentially|likely\s+to\s+be|"
    r"if|whether|depending|depends|assuming|to\s+find\s+out|let(?:'s|\s+us)|"
    r"want\s+me\s+to|would\s+you\s+like|i\s+can\s+(?:check|run|walk)|"
    r"can\s+(?:i|we)\s+(?:check|run)|check\s+(?:if|whether|your))\b",
    re.IGNORECASE,
)

# A condition ATTACHED to the verdict ("...eligible for Medicaid if your
# income is under X") still hedges it even though it trails the claim. One
# that sits in its own clause ("...eligible for Medicaid; if you apply this
# month, coverage starts right away") does not.
_ELIGIBILITY_TRAILING_CONDITION_REGEX: Pattern[str] = re.compile(
    r"\b(?:if|unless|as\s+long\s+as|provided|assuming|depending|whether)\b",
    re.IGNORECASE,
)

# What separates the verdict from a following clause. A trailing condition
# only reaches back to the claim when nothing like this stands between them.
_CLAUSE_BREAK_REGEX: Pattern[str] = re.compile(r"[,;:]|--|\u2014")

# Sentence-ish split: a terminator followed by whitespace, or a line break.
# Bullet lists arrive one verdict per line, so newlines have to split too.
_SENTENCE_SPLIT_REGEX: Pattern[str] = re.compile(r"(?<=[.!?])\s+|\n+")


def detect_eligibility_verdict(text: Optional[str]) -> bool:
    """
    Check whether a reply flatly ASSERTS a Medicaid/Medicare eligibility verdict.

    Returns True only for definite claims. A sentence is ignored when it is a
    question, when hedging wording SCOPES the claim (anything before the end
    of the verdict itself), or when a condition is attached directly to it, so
    offering the eligibility check ("you may be eligible -- want me to
    check?") and stating a rule ("you are eligible for Medicaid if your income
    is under 138% FPL") both read as clean.

    Hedging that trails the verdict in its own clause does NOT clear it: the
    system prompt requires an experimental/confirm-with-your-state caveat on
    every eligibility answer, so treating those as hedges let essentially
    every invented verdict through.

    Whether such a verdict is legitimate is the CALLER's call: after the
    medicaid_eligibility checker has run, restating its determination is the
    whole point. See score_llm_response, which only penalizes a verdict the
    checker never computed.
    """
    if not text:
        return False
    for sentence in _SENTENCE_SPLIT_REGEX.split(text):
        match = _ELIGIBILITY_VERDICT_REGEX.search(sentence)
        if not match:
            continue
        if sentence.rstrip().endswith("?"):
            continue
        # Hedging that scopes the claim: everything up to and including the
        # verdict wording itself.
        if _ELIGIBILITY_HEDGE_REGEX.search(sentence[: match.end()]):
            continue
        # A condition attached to the verdict, i.e. before any clause break.
        trailing = sentence[match.end() :]
        clause_break = _CLAUSE_BREAK_REGEX.search(trailing)
        attached = trailing[: clause_break.start()] if clause_break else trailing
        if _ELIGIBILITY_TRAILING_CONDITION_REGEX.search(attached):
            continue
        return True
    return False

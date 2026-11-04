"""Does the person's own message ask whether THEY can get Medicaid or Medicare?

The chat's eligibility check only runs when the model emits a
``medicaid_eligibility`` tool call, and the system prompt tells it to do so
the moment someone asks whether they qualify. Models skip that step often
enough to matter: they answer from memory, run their own interview, or hand
down a verdict the checker never computed (the invented-verdict penalty in
chat/llm_client.py exists because of the last one). This module reads the
REQUEST side -- the user's message -- so a turn can

* tell the model, in the message itself, to start the check
  (``ELIGIBILITY_REQUEST_NOTE``), and
* start it anyway when the model's reply didn't
  (``ChatInterface._inject_eligibility_call``).

``detect_eligibility_request`` is deliberately narrow. It fires on sentences
whose subject is the person (or someone they are asking for) and whose object
is one of the programs: "am I eligible for Medicaid", "see if my mom
qualifies for Medicare", "will I lose my Medi-Cal", "do the work
requirements apply to me". It does NOT fire on general questions ("what is
Medicaid?", "what are the income limits?"), which the prompt answers first
and follows with an OFFER of the check, nor on "eligible to appeal", nor on
a professional's "eligibility check" of a patient's insurance, which is a
different thing with the same name. Erring toward not matching is the safe
direction: a missed request leaves the prompt's own instruction in charge,
while a false positive starts an interview nobody asked for.

Stdlib + medicaid_names at import time; the state scan imports medicaid_api
(pandas) lazily, the same way the chat tools do.
"""

import json
import re
from typing import Iterable, List, NamedTuple, Optional, Pattern

from fighthealthinsurance.ml.medicaid_names import (
    AMBIGUOUS_PROGRAM_ALIASES,
    MEDICAID_PROGRAM_ALIASES,
    MEDICAID_PROGRAM_STATES,
)


class EligibilityRequest(NamedTuple):
    """One recognized request to check eligibility."""

    # The program the sentence named, as written ("Medi-Cal", "medicare").
    # Informational: the checker scores Medicaid and Medicare together.
    program: str
    # Lowercase postal code when the message itself names a state (or a
    # one-state program like Medi-Cal), else None.
    state: Optional[str]


# Programs a request can be about. The ambiguous aliases are left out for
# the same reason safety_filters leaves them out of verdicts: "do I qualify
# for medical assistance" and "...for STAR" are not reliably about Medicaid.
# Longest-first so the alternation prefers "Medicaid expansion" over
# "Medicaid" and "BadgerCare Plus" over "BadgerCare".
_PROGRAM_NAMES: tuple[str, ...] = tuple(
    sorted(
        {"Medicaid", "Medicare", "CHIP", "Medicaid expansion"}
        | {
            name
            for name in MEDICAID_PROGRAM_ALIASES
            if name not in AMBIGUOUS_PROGRAM_ALIASES
        },
        key=len,
        reverse=True,
    )
)
_PROGRAM = "(?:" + "|".join(re.escape(n) for n in _PROGRAM_NAMES) + ")"
# The program a matched sentence named: the LAST program mention in the
# matched span is its object ("...whether I qualify for Medi-Cal"). Unnamed
# groups above because the alternation below embeds _PROGRAM several times,
# and a named group may appear only once per pattern.
_PROGRAM_ONLY_REGEX: Pattern[str] = re.compile(rf"\b{_PROGRAM}\b", re.IGNORECASE)

# "eligible for Medicaid coverage OF a wheelchair" is a benefits question,
# not an eligibility one.
_NOT_A_BENEFIT = r"(?!\s*(?:coverage|reimbursement|payment|benefits?)\s+(?:of|for)\b)"

# Up to two words between "for" and the program: "eligible for free Medicaid",
# "qualify for California Medi-Cal", "eligible for coverage under Medicaid".
_FOR_PROGRAM = rf"\s+for\s+(?:\w+[-\s]+){{0,2}}?{_PROGRAM}\b{_NOT_A_BENEFIT}"

# Who is asking: themselves, their household, or someone they speak for
# ("my mom", "my 70 year old mother", "my patient").
_SUBJECT = r"(?:i|we|she|he|they|my\s+(?:\w+\s+){0,3}?\w+)"
_ADVERB = r"(?:still|even|possibly|likely|actually|really|ever|also|now)"
_ELIGIBLE = r"(?:eligible|qualif(?:y|ies|ied))"

_REQUEST_PATTERNS: List[str] = [
    # "am I eligible for Medicaid", "would my mom qualify for Medicare",
    # "do I even qualify for Medi-Cal", "is my patient eligible for MassHealth"
    rf"\b(?:am|is|are|would|could|can|will|might|do|does|should)\s+{_SUBJECT}"
    rf"\s+(?:{_ADVERB}\s+)*(?:be\s+)?(?:{_ADVERB}\s+)*{_ELIGIBLE}{_FOR_PROGRAM}",
    # "check whether I might be eligible for Medicaid" (the landing page's
    # own first message), "see if I qualify for Medi-Cal", "not sure if I'm
    # eligible for medicaid", "wondering if my mom qualifies for Medicare"
    rf"\b(?:if|whether)\s+{_SUBJECT}"
    r"(?:'m|'re|'s|\s+(?:am|is|are|would|could|might|may|will|can|do|does))?"
    rf"(?:\s+{_ADVERB})*(?:\s+be)?(?:\s+{_ADVERB})*\s+{_ELIGIBLE}{_FOR_PROGRAM}",
    # "my eligibility for Medicaid", "my mother's eligibility for Medicare"
    rf"\b(?:my|our|his|her|their)\s+(?:\w+'s\s+)?eligibility{_FOR_PROGRAM}",
    # "my Medicaid eligibility", "run the Medi-Cal eligibility check"
    rf"\b(?:my|our|his|her|their)\s+{_PROGRAM}\s+eligibility\b",
    rf"\b{_PROGRAM}\s+eligibility\s+(?:check|checker|screen(?:ing)?|test|tool|estimate|assessment)\b",
    # "will I lose my Medicaid", "can I keep Medi-Cal", "am I going to get
    # kicked off Medicaid"
    rf"\b(?:will|would|could|might|am|do|can)\s+(?:i|we)\s+"
    r"(?:still\s+|going\s+to\s+|gonna\s+)?"
    r"(?:lose|keep|losing|keeping|stay\s+on|remain\s+on|still\s+have|still\s+get|"
    r"still\s+qualify\s+for|get\s+kicked\s+off(?:\s+of)?)"
    rf"\s+(?:my\s+|our\s+|the\s+)?{_PROGRAM}\b",
    # "can I get Medicaid", "could we get on Medi-Cal" -- but not "can I get
    # Medicaid to pay for X", which is a coverage question.
    rf"\b(?:can|could|would|will|might|do)\s+(?:i|we)\s+(?:{_ADVERB}\s+)?"
    r"(?:get|get\s+on(?:to)?|go\s+on|be\s+on|sign\s+up\s+for|enroll\s+in)\s+"
    rf"(?:free\s+|the\s+)?{_PROGRAM}\b(?!\s+to\s+(?:pay|cover|reimburse))",
]

# The work requirement is Medicaid's alone, so "do the work requirements
# apply to me" needs no program name to be an eligibility question -- and the
# prompt lists it as one only the checker can answer.
_WORK_REQUIREMENT_PATTERN = (
    r"\bwork\s+(?:requirements?|rules?)\b[^.?!\n]{0,40}?"
    r"\b(?:apply\s+to|affect|impact|cover)\s+(?:me|us|my)\b"
)

_REQUEST_REGEX: Pattern[str] = re.compile(
    "|".join(f"(?:{p})" for p in _REQUEST_PATTERNS), re.IGNORECASE
)
_WORK_REQUIREMENT_REGEX: Pattern[str] = re.compile(
    _WORK_REQUIREMENT_PATTERN, re.IGNORECASE
)

# One-state program names, longest first, for the state scan.
_PROGRAM_STATE_REGEX: Pattern[str] = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(name)
        for name in sorted(MEDICAID_PROGRAM_STATES, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
)
_PROGRAM_STATE_BY_LOWER = {
    name.lower(): code for name, code in MEDICAID_PROGRAM_STATES.items()
}

# A postal code counts only in UPPER case and only after a word that makes
# it a place ("in CA", "from TX", "Los Angeles, CA"): the lower-case codes
# are too many ordinary words ("in or near", "live in me", "is it ok").
_POSTAL_CODE_REGEX: Pattern[str] = re.compile(
    r"(?:\b(?:in|from|of|live\s+in|living\s+in|here\s+in|state\s+is|state\s+of)\s+|,\s*)"
    r"([A-Z]{2})\b"
)

# The first time a chat asks, the model gets the message-level note; an
# ongoing interview gets the second. Both are system notes appended to the
# user's message, the same shape as the terse-reply bridge, so the rules
# around the message (repeat detection on the RAW message, prompt wrapping)
# are unaffected.
ELIGIBILITY_REQUEST_NOTE = (
    "\n\n[Note from the system, not the user: the user is asking whether "
    "they (or someone they are asking for) can get Medicaid or Medicare. "
    "Run our EXPERIMENTAL eligibility check now: reply with ONLY the "
    "**medicaid_eligibility {...}** tool call, carrying every detail they "
    "have given so far (state, age, household size, monthly income, and so "
    "on) and nothing they have not. Do not ask eligibility questions "
    "yourself and do not state or guess a verdict -- the tool tells you "
    "what to ask next, and only its answer may be given.]"
)
ELIGIBILITY_INTERVIEW_NOTE = (
    "\n\n[Note from the system, not the user: an EXPERIMENTAL "
    "Medicaid/Medicare eligibility check is underway in this chat and has "
    "not reached a verdict. If this message answers the check's questions "
    "or asks about their eligibility, reply with ONLY the "
    "**medicaid_eligibility {...}** tool call, carrying the new answers "
    "together with everything recorded so far; do not ask your own "
    "eligibility questions and do not state or guess a verdict. If the user "
    "has moved on to something else, help with that instead.]"
)


def detect_eligibility_request(text: Optional[str]) -> Optional[EligibilityRequest]:
    """The eligibility request in ``text``, or None when it makes none.

    See the module docstring for what counts. The returned ``state`` is
    whatever ``state_mentioned_in`` finds in the same message.
    """
    if not text:
        return None
    match = _REQUEST_REGEX.search(text)
    if match:
        programs = _PROGRAM_ONLY_REGEX.findall(match.group(0))
        return EligibilityRequest(
            program=programs[-1] if programs else "Medicaid",
            state=state_mentioned_in(text),
        )
    if _WORK_REQUIREMENT_REGEX.search(text):
        return EligibilityRequest(program="Medicaid", state=state_mentioned_in(text))
    return None


def _state_name_regex() -> Pattern[str]:
    """Full state names (and the DC spellings), longest first, word-bounded.

    Built on first use: it needs medicaid_api's state table, and that module
    imports pandas, which the chat path should not pay for until a Medicaid
    turn actually happens.
    """
    global _STATE_NAME_REGEX
    if _STATE_NAME_REGEX is None:
        from fighthealthinsurance.medicaid_api import _STATE_MAP

        names = sorted(
            (name for name in _STATE_MAP if len(name) > 2), key=len, reverse=True
        )
        _STATE_NAME_REGEX = re.compile(
            r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE
        )
    return _STATE_NAME_REGEX


_STATE_NAME_REGEX: Optional[Pattern[str]] = None


def state_mentioned_in(text: Optional[str]) -> Optional[str]:
    """The one US state ``text`` names, as a lowercase postal code.

    Recognizes a one-state program name ("Medi-Cal" -> ca), a full state
    name, and an upper-case postal code after a place word ("in CA"). Returns
    None when the text names no state -- or more than one ("I moved from
    Texas to California"): a wrong state scores the person under the wrong
    rules, so the checker is left to ask.
    """
    if not text:
        return None
    from fighthealthinsurance.medicaid_api import _STATE_MAP

    found: set[str] = set()
    for match in _PROGRAM_STATE_REGEX.finditer(text):
        found.add(_PROGRAM_STATE_BY_LOWER[match.group(0).lower()])
    for match in _state_name_regex().finditer(text):
        found.add(_STATE_MAP[match.group(0).lower()])
    codes = frozenset(_STATE_MAP.values())
    for match in _POSTAL_CODE_REGEX.finditer(text):
        code = match.group(1).lower()
        if code in codes:
            found.add(code)
    if len(found) == 1:
        return next(iter(found))
    return None


def state_from_user_messages(messages: Iterable[Optional[str]]) -> Optional[str]:
    """The state named by the most recent of ``messages`` that names one.

    ``messages`` are the USER's own messages, newest first: what the person
    said is confirmed by definition, where an IP-derived hint (the chat's
    state_hint) is not and must never be sent to the checker as a fact.
    """
    for message in messages:
        state = state_mentioned_in(message)
        if state:
            return state
    return None


def build_eligibility_call(state: Optional[str]) -> str:
    """The ``medicaid_eligibility`` tool call to inject, in the prompt's own
    ``**tool {JSON}**` shape, carrying the state when one is known.

    Only the state: everything else the person may have said (age, income)
    is the model's to parse, and the checker asks for what it lacks. A call
    with no state makes the checker ask for it first.
    """
    payload = {"state": state} if state else {}
    return f"**medicaid_eligibility {json.dumps(payload)}**"

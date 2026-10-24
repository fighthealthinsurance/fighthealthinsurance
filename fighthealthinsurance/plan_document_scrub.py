"""Taking personal details out of the text of shared plan documents, as best
we can.

A plan document is mostly about the plan: what it covers, what it costs, who
runs it. Some carry a person's details too, on a cover letter or a filled-in
form. :func:`scrub` replaces what it finds with ``[removed]``:

* email addresses, and phone numbers other than toll-free ones (800, 833,
  844, 855, 866, 877 and 888, which are the plan's own lines), found the way
  ml/letter_quality finds them;
* the value after a label naming a person or their records: Member ID,
  Subscriber ID, Claim number, Patient, Member name, Name, Date of birth or
  DOB, SSN or Social Security, and an address after "Address:";
* a Social Security number written as 123-45-6789 anywhere;
* the name after "Dear" at the start of a letter.

It keeps what makes the document useful: group numbers, plan and policy
numbers, plan names, the employer and insurer, dollar amounts and the dates
of a coverage period. It is a reduction, not de-identification: a name or an
address with no label in front of it goes through. Pure Python with no model
calls; every pattern is a bounded scan, since the text can run to millions
of characters.
"""

import bisect
import re

from fighthealthinsurance.ml.letter_quality import _PHONE_RE, _email_spans, _phone_key

REMOVED = "[removed]"

#: Toll-free area codes, used by plans for their member and provider lines.
TOLL_FREE_AREA_CODES = frozenset({"800", "833", "844", "855", "866", "877", "888"})

_FLAGS = re.IGNORECASE | re.MULTILINE
# Spaces or tabs, never a line break: a label's value is on its own line.
_SP = r"[^\S\n]"
# What sits between a label and its value: spaces, then at most one of : # . -
# Each run of spaces has one quantifier, so a long run cannot backtrack.
_SEP = rf"{_SP}*(?:[:#.-]{_SP}*)?"
_PERSON = (
    r"(?:member|patient|subscriber|insured|enrollee|employee|dependent"
    rf"|beneficiary|participant|policy{_SP}?holder)"
)
# The person labels that also stand alone before a colon ("Patient: ...").
_PERSON_ALONE = (
    rf"(?:member|patient|subscriber|insured|enrollee|dependent|policy{_SP}?holder)"
)
_NUMBER_WORD = r"(?:id(?:entification)?|identifier|number|no\b\.?|#)"

# An identifier: up to five letters and a space in front, then a run of
# letters, digits and dashes holding at least one digit.
_ID_VALUE = rf"(?P<value>(?:[a-z]{{1,5}}{_SP})?[a-z0-9-]{{0,29}}\d[a-z0-9-]{{0,29}})"
_ID_RULE = re.compile(
    rf"(?:\b(?:{_PERSON}|claim)(?:'s)?{_SP}*{_NUMBER_WORD}"
    rf"(?:{_SP}*(?:number|no\b\.?|#))?|^{_SP}*id\b){_SEP}{_ID_VALUE}",
    _FLAGS,
)

_SSN_RULE = re.compile(
    rf"\b(?:ssn|ss#|social{_SP}+security(?:{_SP}+(?:number|no\b\.?|#))?){_SEP}"
    rf"(?P<value>[\dx*](?:[\dx*-]|{_SP}){{6,12}}[\dx*])",
    _FLAGS,
)
_BARE_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")

_DATE = (
    r"(?:\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}"
    rf"|[a-z]{{3,9}}\.?{_SP}+\d{{1,2}},?{_SP}+\d{{2,4}}"
    rf"|\d{{1,2}}{_SP}+[a-z]{{3,9}}\.?,?{_SP}+\d{{2,4}})"
)
_DOB_RULE = re.compile(
    rf"(?:\bdate{_SP}+of{_SP}+birth|\bbirth{_SP}*date|\bd\.?o\.?b\b\.?){_SEP}"
    rf"(?P<value>{_DATE})",
    _FLAGS,
)

# A name is one to four words, each starting with a capital letter, one
# space apart, stopping before a word that starts the next label. Matched
# with case, so "Patient: you pay 20%" is left alone; the labels ignore it.
_NEXT_LABEL = r"(?i:(?!(?:dob|date|id|ssn|member|group|address|phone|plan|policy)\b))"
_CAPITALISED = r"[^\W\d_a-z][\w'.-]"
_NAME_VALUE = rf"(?P<value>{_CAPITALISED}{{1,30}}(?:{_SP}{_NEXT_LABEL}{_CAPITALISED}{{0,30}}){{0,3}})"
_NAME_RULE = re.compile(
    rf"(?i:\b{_PERSON}(?:'s)?{_SP}+name(?:{_SP}*:)?|\b{_PERSON_ALONE}{_SP}*:"
    rf"|^{_SP}*name{_SP}*:){_SP}*{_NAME_VALUE}",
    re.MULTILINE,
)
_DEAR_RULE = re.compile(rf"^{_SP}*(?i:dear){_SP}+{_NAME_VALUE}", re.MULTILINE)
# Salutations that name nobody.
_GENERIC_SALUTATIONS = frozenset(
    {
        "member",
        "members",
        "customer",
        "participant",
        "participants",
        "subscriber",
        "enrollee",
        "valued",
        "sir",
        "madam",
        "provider",
        "patient",
        "policyholder",
        "beneficiary",
    }
)

_ADDRESS_RULE = re.compile(
    rf"(?:\b(?:home|member|patient|subscriber|employee|your|street|residential)"
    rf"{_SP}+address|^{_SP}*address){_SP}*:{_SP}*(?P<value>\S[^\n]{{0,99}})",
    _FLAGS,
)

# Plan facts kept even where they look like a phone number or an identifier.
_KEPT_RULE = re.compile(
    rf"\b(?:group|plan|policy|payer|rx{_SP}*(?:bin|pcn|grp|group))\b"
    rf"(?:{_SP}*{_NUMBER_WORD})?{_SEP}(?P<value>[a-z0-9-]{{1,30}})",
    _FLAGS,
)


def _values(rule: re.Pattern, text: str) -> list[tuple[int, int]]:
    return [m.span("value") for m in rule.finditer(text)]


def _names(text: str) -> list[tuple[int, int]]:
    spans = _values(_NAME_RULE, text)
    for match in _DEAR_RULE.finditer(text):
        first = match.group("value").split()[0].strip(".,").lower()
        if first not in _GENERIC_SALUTATIONS:
            spans.append(match.span("value"))
    return spans


def _phones(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _PHONE_RE.finditer(text):
        digits = _phone_key(match.group())
        if len(digits) == 10 and digits[:3] in TOLL_FREE_AREA_CODES:
            continue
        spans.append(match.span())
    return spans


def _overlaps_kept(span: tuple[int, int], kept: list[tuple[int, int]]) -> bool:
    # kept is sorted and its spans never overlap, so only the one starting
    # just before span's end can reach into it.
    at = bisect.bisect_left(kept, (span[1], -1)) - 1
    return at >= 0 and kept[at][1] > span[0]


def removal_spans(text: str) -> list[tuple[int, int]]:
    """Where :func:`scrub` replaces text: sorted, merged, and clear of the
    plan facts it keeps."""
    kept = _values(_KEPT_RULE, text)
    found = (
        _email_spans(text)
        + _phones(text)
        + _values(_ID_RULE, text)
        + _values(_SSN_RULE, text)
        + [m.span() for m in _BARE_SSN.finditer(text)]
        + _values(_DOB_RULE, text)
        + _names(text)
        + _values(_ADDRESS_RULE, text)
    )
    spans: list[tuple[int, int]] = []
    for span in sorted(found):
        if _overlaps_kept(span, kept):
            continue
        if spans and span[0] <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], span[1]))
        else:
            spans.append(span)
    return spans


def scrub(text: str) -> str:
    """``text`` with the personal details found replaced by ``[removed]``."""
    if not text:
        return ""
    pieces = []
    cursor = 0
    for start, end in removal_spans(text):
        pieces.append(text[cursor:start])
        pieces.append(REMOVED)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)

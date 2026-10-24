"""Taking personal details out of the text of shared plan documents.

A plan document is mostly about the plan: what it covers, what it costs, who
runs it. Some carry a person's details too, on a cover letter, an ID card or
a filled-in form. :func:`scrub` replaces these with ``[removed]``:

* email addresses, and phone numbers other than toll-free ones (800, 833,
  844, 855, 866, 877 and 888, which are the plan's own lines), found the way
  ml/letter_quality finds them;
* the value after a label naming a person or their records, on the label's
  line or, when the label ends its line, on the next one: member,
  subscriber, cardholder, beneficiary, recipient and other IDs and numbers
  ("ID#", "ID Number"), Medicare and Medicaid numbers and MBIs, policy and
  claim numbers, patient account numbers, names (also "Surname, Given"),
  dates of birth, Social Security numbers, and a member's address after
  "Address", "Home Address", "Mailing Address" and the like, with or
  without a colon;
* a Social Security number written as 123-45-6789 anywhere;
* the name after "Dear" at the start of a letter, unless the greeting is
  to everyone ("Dear Member", "Dear Plan Participant").

It keeps what makes the document useful: group numbers, plan names and plan
IDs, Rx BIN, PCN and group, payer IDs, provider NPIs, the employer and
insurer, dollar amounts and the dates of a coverage period. Pure Python with no model calls;
every pattern is a bounded scan, with one quantifier for each run of spaces,
since the text can run to millions of characters. Best effort: the tests
show what it catches.
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
# A label line that ends after its label: the value is on the next line.
_NEXT_LINE = rf"(?:\n{_SP}*)?"
_PERSON = (
    r"(?:member|patient|subscriber|insured|enrollee|employee|dependent"
    rf"|beneficiary|participant|cardholder|recipient|policy{_SP}?holder)"
)
# The person labels that also stand alone before a colon ("Patient: ...").
_PERSON_ALONE = (
    r"(?:member|patient|subscriber|insured|enrollee|dependent|beneficiary"
    rf"|cardholder|recipient|policy{_SP}?holder)"
)
_NUMBER_WORD = r"(?:id(?:entification)?|identifier|number|no\b\.?|#)"
# A number word, and a second one as in "ID Number" or "ID #".
_NUMBER_WORDS = rf"{_NUMBER_WORD}(?:{_SP}*(?:number|no\b\.?|#))?"
# Whose number it is: a person's, or one of their records.
_RECORD = rf"(?:{_PERSON}|claim|policy|medicare|medicaid|patient{_SP}+account)"

# An identifier: up to five letters and a space in front, then a run of
# letters, digits and dashes holding at least one digit, and up to three
# more such runs one space apart ("W123 456 789").
_ID_PART = r"[a-z0-9-]{0,29}\d[a-z0-9-]{0,29}"
_ID_VALUE = (
    rf"(?P<value>(?:[a-z]{{1,5}}{_SP})?{_ID_PART}(?:{_SP}{_ID_PART}(?![\w/-])){{0,3}})"
)
_ID_RULE = re.compile(
    rf"(?:\b{_RECORD}(?:'s)?{_SP}*{_NUMBER_WORDS}|\bpolicy(?={_SP}*:)|\bmbi\b"
    rf"|\bid(?:entification)?{_SP}*(?:number|no\b\.?|#)|^{_SP}*id\b)"
    rf"{_SEP}{_NEXT_LINE}{_ID_VALUE}",
    _FLAGS,
)

_SSN_RULE = re.compile(
    rf"\b(?:ssn|ss#|social{_SP}+security(?:{_SP}+(?:number|no\b\.?|#))?){_SEP}{_NEXT_LINE}"
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
    rf"(?:\bdate{_SP}+of{_SP}+birth|\bbirth{_SP}*date|\bd\.?o\.?b\b\.?){_SEP}{_NEXT_LINE}"
    rf"(?P<value>{_DATE})",
    _FLAGS,
)

# A name is one to four words, each starting with a capital letter, one
# space apart, never a word that starts a label. Matched with case, so
# "Patient: you pay 20%" is left alone; the labels ignore it.
_NEXT_LABEL = (
    r"(?i:(?!(?:dob|date|id|ssn|member|subscriber|patient|cardholder|medicare"
    r"|medicaid|mbi|claim|group|address|phone|plan|policy)\b))"
)
_CAPITALISED = rf"{_NEXT_LABEL}[^\W\d_a-z][\w'.-]"
_FULL_NAME = rf"{_CAPITALISED}{{1,30}}(?:{_SP}{_CAPITALISED}{{0,30}}){{0,3}}"
# After a label a name can also be "Surname, Given", as on ID cards.
_NAME_VALUE = (
    rf"(?P<value>{_FULL_NAME}"
    rf"(?:,{_SP}?{_CAPITALISED}{{0,30}}(?:{_SP}{_CAPITALISED}{{0,30}}){{0,2}})?)"
)
_NAME_RULE = re.compile(
    rf"(?i:\b{_PERSON}(?:'s)?{_SP}+name(?:{_SP}*:)?|\b{_PERSON_ALONE}{_SP}*:"
    rf"|^{_SP}*name{_SP}*:){_SP}*{_NEXT_LINE}{_NAME_VALUE}",
    re.MULTILINE,
)
_DEAR_RULE = re.compile(rf"^{_SP}*(?i:dear){_SP}+(?P<value>{_FULL_NAME})", re.MULTILINE)
# Salutations that name nobody.
_GENERIC_SALUTATIONS = frozenset(
    {
        "member",
        "members",
        "customer",
        "customers",
        "participant",
        "participants",
        "subscriber",
        "subscribers",
        "employee",
        "employees",
        "enrollee",
        "enrollees",
        "retiree",
        "retirees",
        "insured",
        "health",
        "valued",
        "sir",
        "madam",
        "colleague",
        "colleagues",
        "parent",
        "parents",
        "guardian",
        "friend",
        "friends",
        "provider",
        "providers",
        "patient",
        "patients",
        "policyholder",
        "policyholders",
        "beneficiary",
        "beneficiaries",
    }
)

# A member's address, the rest of its line. Labels the plan uses for its own
# mail ("Send appeals to", "Plan address") are not among these. With no
# colon the value has to start with a street or box number.
_ADDRESS_RULE = re.compile(
    rf"(?:\b(?:home|mailing|member|patient|subscriber|employee|your|street"
    rf"|residential){_SP}+address(?:{_SP}+on{_SP}+file)?|^{_SP}*address)"
    rf"{_SP}*(?P<colon>:{_SP}*)?{_NEXT_LINE}"
    rf"(?P<value>(?(colon)\S|\d)[^\n]{{0,99}})",
    _FLAGS,
)

# Plan facts kept even where they look like a phone number or an identifier.
# A provider's NPI is ten digits, like a phone number, and names no member.
_KEPT_RULE = re.compile(
    rf"\b(?:group|plan|payer|npi|rx{_SP}*(?:bin|pcn|grp|group))\b"
    rf"(?:{_SP}*{_NUMBER_WORDS})?{_SEP}(?P<value>[a-z0-9-]{{1,30}})",
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


def _inside_kept(span: tuple[int, int], kept: list[tuple[int, int]]) -> bool:
    # kept is sorted and disjoint, so only one span can hold it.
    at = bisect.bisect_right(kept, span[0], key=lambda kept_span: kept_span[0]) - 1
    return at >= 0 and kept[at][1] >= span[1]


def removal_spans(text: str) -> list[tuple[int, int]]:
    """Where :func:`scrub` replaces text: sorted and merged. A detail wholly
    inside a plan fact it keeps stays; one that runs past it goes."""
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
        if _inside_kept(span, kept):
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

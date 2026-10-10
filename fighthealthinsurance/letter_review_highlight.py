"""What the letter review page highlights in a letter, for the reader's eye.

Plain string matching, no model: a letter and its input always give the
same highlights, so both readers see the same thing. Specifics in the letter
(numbers, money, dates, codes, citations, names) are highlighted when the
same text is not in the input the writer saw; fill-ins such as [Date] get
their own style. It is a reading aid, not a verdict.
"""

import re
from dataclasses import dataclass
from typing import FrozenSet, List, Tuple

from django.views.decorators.debug import sensitive_variables

from fighthealthinsurance.letter_placeholders import find_placeholder_spans

UNMATCHED = "unmatched"
FILL_IN = "fill-in"

# Shown on the page under the letter, so readers know what lights up.
RULES = (
    "Highlighted: numbers of two or more digits, dollar amounts, dates, codes "
    "and IDs (letters and digits mixed, or digit groups such as 123-45-6789), "
    "citations (§, U.S.C., C.F.R.) and DOIs, and names of two or more "
    "capitalised words, when the same text isn't in the input.",
    "A number counts as in the input only where the input has it on its own, "
    "as dollars, or in a range written with commas or $ (1,200-1,500, "
    "$100-$200). Inside a date (12/03/2024), a code (ICD-10) or digit groups "
    "(123-45-6789, 18-64) it doesn't: those are read whole.",
    "Matching ignores case, spacing and the commas and zero cents in numbers. "
    "A date or name written another way in the input is still highlighted.",
    "Everyday letter words in capitals (Dear, Appeals Department, Medical "
    "Necessity and the like) are not taken for names.",
    "Fill-ins such as [Date] have their own style.",
    "A highlight is a pointer, not a verdict, and no highlight doesn't mean a "
    "claim is supported.",
)

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
# Most specific first: a date is not also read as its numbers. Within a
# kind, alternatives are tried in order, so the fuller form wins.
_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("doi", re.compile(r"\b10\.\d{4,9}/[^\s\])>,;]+")),
    (
        "citation",
        re.compile(
            r"\b\d+\s+(?:U\.S\.C\.|USC|C\.F\.R\.|CFR)\s*(?:§+\s*)?[\dA-Za-z.()-]+"
            r"|§+\s*[\dA-Za-z.()-]+"
        ),
    ),
    ("money", re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")),
    (
        "date",
        re.compile(
            rf"\b{_MONTH}\.?\s+{_DAY},?\s+\d{{4}}\b"
            rf"|\b{_DAY}\s+{_MONTH}\.?,?\s+\d{{4}}\b"
            rf"|\b{_MONTH}\.?\s+\d{{4}}\b"
            rf"|\b{_MONTH}\.?\s+{_DAY}\b"
            r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
            r"|\b\d{1,2}-\d{1,2}-\d{4}\b"
            r"|\b\d{4}-\d{1,2}-\d{1,2}\b"
        ),
    ),
    (
        "code",
        re.compile(
            # Letters and digits joined by - or . (ICD-10, M54.5, ZX9-4471).
            r"\b(?=[A-Za-z0-9.-]*\d)(?=[A-Za-z0-9.-]*[A-Za-z])"
            r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)+\b"
            # Five or more letters and digits mixed (CLM12345).
            r"|\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{5,}\b"
            # Three or four, starting with a capital (I10, J45, M545), so an
            # ordinal like 2nd is not one.
            r"|\b[A-Z](?=[A-Za-z]*\d)[A-Za-z0-9]{2,3}\b"
            # Digit groups (123-45-6789, 555-1234), but not a comma range.
            r"|(?<![\d,.$])\d+(?:[-/]\d+)+(?![\d,])"
        ),
    ),
    ("number", re.compile(r"\b\d[\d,]*(?:\.\d+)?%?")),
)
_NUMBER_KINDS = ("money", "number")
_NAME_PRIORITY = len(_PATTERNS)

# Capitalised words a letter uses that are not names on their own.
_NOT_NAMES = frozenset("""
    a an and appeal appeals at authorization benefit benefits board by care
    claim company concern coverage date de dear denial department dr external
    for health i in independent insurance internal it la medical member may my
    name necessity of on our patient physician plan please policy prior
    provider re regarding request review services service sincerely subject
    thank the this to van von we whom you your
    """.split())

# Words that start a sentence or a title, trimmed off the front of a name so
# "Then Mary Walker" and "Dr. Mary Walker" match "Mary Walker".
_NAME_STARTERS = frozenset("""
    after and as at before but by dear dr during for from her his if in miss
    mr mrs ms my on our per prof since that the their then this to when while
    with your
    """.split())
# Lower-case words that can sit inside a name ("Bank of America").
_CONNECTORS = frozenset("of and the for de del da di van von la le".split())
# A word, in any script, with any hyphens or apostrophes inside it.
_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?")
_TITLES = frozenset("dr mr mrs ms prof st".split())

_SPACES = re.compile(r"\s+")
_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")


@dataclass(frozen=True)
class Segment:
    text: str
    kind: str = ""


def _plain(text: str) -> str:
    return _SPACES.sub(" ", text).strip().lower()


def _digits(text: str) -> str:
    """A number as compared: no $, no commas between digits, no zero cents."""
    value = _DIGIT_COMMA.sub("", text).replace("$", "").strip()
    return re.sub(r"\.0+(?=%?$)", "", value)


def _trim(token: str, kind: str) -> str:
    """The token without what surrounds it in the sentence: a full stop or
    comma, a closing bracket it didn't open, or a name's possessive."""
    while True:
        before = token
        token = token.rstrip(".,;:")
        if token.endswith(")") and token.count(")") > token.count("("):
            token = token[:-1]
        if kind == "name":
            token = re.sub(r"['’]s$", "", token)
        if token == before:
            return token


def _ends_sentence(word: str) -> bool:
    """A word with a full stop that ends its sentence: not a title or an
    initial, which a name runs on past."""
    if not word.endswith("."):
        return False
    bare = word[:-1]
    return bare.lower() not in _TITLES and len(bare) > 1


def _is_name(words: List[str]) -> bool:
    """Two or more capitalised words, not all everyday letter words. In
    capitals throughout (JOHN SMITH) none may be one, so a heading like
    APPEAL OF DENIAL is not a name."""
    capitals = [w.rstrip(".") for w in words if w[0].isupper()]
    if len(capitals) < 2:
        return False
    known = [w.lower() in _NOT_NAMES for w in capitals]
    if all(w.isupper() for w in capitals if len(w) > 1):
        return not any(known)
    return not all(known)


def _name_spans(text: str) -> List[Tuple[int, int]]:
    """Runs of capitalised words on one line, joined by spaces and the odd
    connector, past any starter words."""
    spans: List[Tuple[int, int]] = []
    run: List["re.Match[str]"] = []

    def close() -> None:
        words = list(run)
        # Starters, connectors and everyday letter words at the front are
        # not part of the name ("Patient Rebecca Lee Crumpler").
        while words and words[0].group(0).rstrip(".").lower() in (
            _NAME_STARTERS | _CONNECTORS | _NOT_NAMES
        ):
            words.pop(0)
        while words and words[-1].group(0).lower() in _CONNECTORS:
            words.pop()
        if words and _is_name([w.group(0) for w in words]):
            spans.append((words[0].start(), words[-1].end()))
        run.clear()

    for match in _WORD.finditer(text):
        word = match.group(0)
        capital = word[0].isupper()
        joined = (
            bool(run)
            and re.fullmatch(r"[ \t]+", text[run[-1].end() : match.start()])
            and not _ends_sentence(run[-1].group(0))
        )
        if joined and (capital or word.lower() in _CONNECTORS):
            run.append(match)
            continue
        close()
        if capital:
            run.append(match)
    close()
    return spans


def _candidates(text: str) -> List[Tuple[int, int, int, str]]:
    """(start, end, priority, kind) for every possible specific in text."""
    found: List[Tuple[int, int, int, str]] = []
    for priority, (kind, pattern) in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            token = _trim(match.group(0), kind)
            if kind == "number" and sum(c.isdigit() for c in token) < 2:
                continue
            if token:
                found.append(
                    (match.start(), match.start() + len(token), priority, kind)
                )
    for start, end in _name_spans(text):
        token = _trim(text[start:end], "name")
        found.append((start, start + len(token), _NAME_PRIORITY, "name"))
    return found


def _select(
    found: List[Tuple[int, int, int, str]], taken: List[Tuple[int, int, str]]
) -> List[Tuple[int, int, str]]:
    """The specifics that don't overlap anything taken: the most specific
    kind first, so a date beats a name or a number inside it; then the
    earliest, then the longest."""
    chosen: List[Tuple[int, int, str]] = []
    for start, end, _, kind in sorted(
        found, key=lambda f: (f[2], f[0], -(f[1] - f[0]))
    ):
        if any(start < t_end and t_start < end for t_start, t_end, _ in taken):
            continue
        if any(start < c_end and c_start < end for c_start, c_end, _ in chosen):
            continue
        chosen.append((start, end, kind))
    return chosen


@sensitive_variables()
def _standalone_numbers(prompt: str) -> FrozenSet[str]:
    """The numbers the input gives on their own (or as dollars, or at either
    end of a range), as compared: never one inside a date, ID or code."""
    return frozenset(
        _digits(prompt[start:end])
        for start, end, kind in _select(_candidates(prompt), [])
        if kind in _NUMBER_KINDS
    )


@sensitive_variables()
def _supported(
    kind: str, token: str, prompt_plain: str, numbers: FrozenSet[str]
) -> bool:
    """Whether the input has the token: a number on its own, anything else
    as the whole token, never part of a longer one (AB1234 is not in
    AB12345 or AB1234-7)."""
    if kind in _NUMBER_KINDS:
        return _digits(token) in numbers
    whole = rf"(?<!\w)(?<!\w-){re.escape(_plain(token))}(?!\w|-\w)"
    return bool(re.search(whole, prompt_plain))


@sensitive_variables()
def _spans(letter: str, prompt: str) -> List[Tuple[int, int, str]]:
    """(start, end, kind) for each highlight, in order and never overlapping."""
    taken: List[Tuple[int, int, str]] = [
        (start, end, FILL_IN) for start, end in find_placeholder_spans(letter)
    ]
    prompt_plain = _plain(prompt)
    numbers = _standalone_numbers(prompt)
    checked: dict = {}
    for start, end, kind in _select(_candidates(letter), taken):
        key = (kind, letter[start:end])
        if key not in checked:
            checked[key] = _supported(kind, key[1], prompt_plain, numbers)
        if not checked[key]:
            taken.append((start, end, UNMATCHED))
    return sorted(taken)


@sensitive_variables()
def paragraphs(letter: str, prompt: str) -> List[List[Segment]]:
    """The letter as paragraphs (split on blank lines) of segments, each
    plain, unmatched or a fill-in."""
    letter = letter.replace("\r\n", "\n").replace("\r", "\n")
    spans = _spans(letter, prompt)
    out: List[List[Segment]] = []
    cursor = 0
    for block in _PARAGRAPH_BREAK.split(letter):
        start = letter.index(block, cursor)
        end = start + len(block)
        cursor = end
        if not block.strip():
            continue
        segments: List[Segment] = []
        at = start
        for s_start, s_end, kind in spans:
            if s_end <= start or s_start >= end:
                continue
            s_start, s_end = max(s_start, start), min(s_end, end)
            if s_start > at:
                segments.append(Segment(letter[at:s_start]))
            segments.append(Segment(letter[s_start:s_end], kind))
            at = s_end
        if at < end:
            segments.append(Segment(letter[at:end]))
        out.append(segments)
    return out

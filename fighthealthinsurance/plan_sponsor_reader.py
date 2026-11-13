"""The company behind a shared plan document, and the plan's insurer, year
and type, read by our own model.

Plan documents name their sponsor in many ways ("Plan Sponsor: ...", "This
plan is sponsored by ...", a title, a table in the back), so fixed patterns
miss some and misread others (plan_sponsors.identify_sponsor, kept as the
fallback). Here one of our internal models reads the parts of the document
that name parties and answers in JSON: the employer that sponsors the plan,
its EIN, and the line that says so; and in the same answer the plan's
insurer, its plan year and its type, each with the line that says so.

Nothing it says is taken on trust. A sponsor is used only when:

* the line it quotes is in the document, word for word (spacing and case
  aside), and names the company in full;
* the name is a company, not an insurer from our catalog or a phrase;
* an EIN is nine digits and sits in that line or just after it, before
  another party is named (otherwise it is dropped and the company kept).

An answer whose sponsor fails a check counts as no answer and the next
model is asked; when none passes, the fixed patterns get their turn. The
insurer, year and type come from the answer used, each checked on its own
and dropped alone (checked_facts): its line must be in the document and,
with personal details taken out, still give it; the insurer's name in
full, the year as four digits from EARLIEST_PLAN_YEAR to next year, the
type as one of PolicyDocument.PLAN_CATEGORY_CHOICES with a word that names
that kind of plan.

When a model finds no sponsor in a document longer than its excerpt, it
reads once more: the passages around party words the excerpt left out
(second_excerpt), within the same TOTAL_TIMEOUT_SECONDS.

Only internal models are asked (infer_with_fallback's default), so the
document's text never leaves our servers. The company it finds counts at
once; staff can check it in the admin, where the quoted line is shown,
without anything waiting on them.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import re
from typing import NamedTuple, Optional

from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_sponsors import (
    _OTHER_PARTY,
    EIN_WINDOW_CHARS,
    SOURCE_MODEL,
    Sponsor,
    good_name,
    normalize_name,
    organization_name,
)

#: How much of a document the model reads: the opening pages, the closing
#: pages (where an SPD's plan information section often sits) and the
#: passages around the words that name parties, within one budget that fits
#: our smallest internal model's context with room for its answer.
HEAD_CHARS = 6_000
TAIL_CHARS = 3_000
WINDOW_CHARS = 700
MAX_EXCERPT_CHARS = 14_000
#: The second look at a long document whose excerpt named no sponsor: the
#: passages around party words the excerpt left out, the sponsor's own
#: words first, at most this many characters. One more model call at most.
SECOND_EXCERPT_CHARS = 8_000
#: The quoted line kept as evidence, at most.
EVIDENCE_CHARS = 300
#: The earliest plan year taken from a document; the latest is next year.
EARLIEST_PLAN_YEAR = 2000
#: Words near which a sponsor is named.
_PARTY_WORDS = re.compile(
    r"sponsor|employer\s+identification|\bEIN\b|plan\s+administrator"
    r"|name\s+of\s+(?:the\s+)?plan|welfare\s+benefit|group\s+health\s+plan"
    r"|provided\s+by\s+your\s+employer|offered\s+by",
    re.IGNORECASE,
)
#: The party words that name the sponsor itself, read first the second time.
_SPONSOR_WORDS = re.compile(r"sponsor|employer\s+identification|\bEIN\b", re.IGNORECASE)
_NOT_UNDER_ERISA = (
    r"non-?erisa|not\s+(?:subject\s+to|governed\s+by|covered\s+by)\s+erisa"
    r"|exempt\s+from\s+erisa|not\s+an?\s+erisa"
)
_ERISA_DENIED = re.compile(_NOT_UNDER_ERISA, re.IGNORECASE)
_OFF_EXCHANGE = re.compile(
    r"off[\s-]+(?:the[\s-]+)?(?:exchange|marketplace)"
    r"|outside\s+(?:of\s+)?(?:the\s+)?(?:health\s+insurance\s+)?(?:marketplace|exchange)"
    r"|not\s+(?:sold|offered|purchased|bought|available)\s+(?:on|through|in)\s+"
    r"(?:the\s+)?(?:health\s+insurance\s+)?(?:marketplace|exchange)",
    re.IGNORECASE,
)
#: Words that name each kind of plan (PolicyDocument.PLAN_CATEGORY_CHOICES
#: but "unknown"): a type is kept only when its quoted line holds one.
_PLAN_TYPE_WORDS = {
    "employer_erisa": re.compile(
        r"\berisa\b|employee\s+retirement\s+income\s+security\s+act", re.IGNORECASE
    ),
    "employer_non_erisa": re.compile(
        _NOT_UNDER_ERISA + r"|governmental\s+plan|church\s+plan|government\s+employer",
        re.IGNORECASE,
    ),
    "aca_marketplace": re.compile(
        r"marketplace|\bexchange\b|healthcare\.gov", re.IGNORECASE
    ),
    "medicare_traditional": re.compile(r"\bmedicare\b", re.IGNORECASE),
    "medicare_advantage": re.compile(
        r"medicare\s+advantage|\bpart\s+c\b|\bma-?pd\b", re.IGNORECASE
    ),
    "medicaid_chip": re.compile(
        r"\bmedicaid\b|\bchip\b|children'?s\s+health\s+insurance\s+program"
        r"|\bmedi-?cal\b",
        re.IGNORECASE,
    ),
    "tricare": re.compile(r"\btricare\b", re.IGNORECASE),
    "va": re.compile(r"\bveterans?\b", re.IGNORECASE),
    "individual_off_exchange": _OFF_EXCHANGE,
    "short_term": re.compile(r"\bshort[\s-]*term\b", re.IGNORECASE),
}
#: A line about another plan working alongside Medicare ("if you have
#: Medicare, this plan pays secondary"), which names Medicare without being
#: it. Not "Original Medicare pays primary", which is about Medicare itself.
_ALONGSIDE_MEDICARE = re.compile(
    r"\b(?:this|the|our|your)\s+(?:plan|coverage|policy)\s+(?:pays|is|will\s+pay)"
    r"\s+(?:as\s+)?(?:primary|secondary)|\b(?:primary|secondary)\s+to\s+medicare"
    r"|coordinat\w*\s+(?:of\s+)?benefits|if\s+you\s+(?:have|are\s+(?:eligible|enrolled))"
    r"|eligible\s+for\s+medicare",
    re.IGNORECASE,
)
#: Words in a quoted line that rule a type out though its own words are
#: there: Medicare Advantage, or a plan working alongside Medicare, is not
#: Original Medicare; an off-exchange plan is not a Marketplace one.
_PLAN_TYPE_DENIED: dict[str, tuple[re.Pattern[str], ...]] = {
    "employer_erisa": (_ERISA_DENIED,),
    "aca_marketplace": (_OFF_EXCHANGE,),
    "medicare_traditional": (
        _PLAN_TYPE_WORDS["medicare_advantage"],
        _ALONGSIDE_MEDICARE,
    ),
}
#: Words that make a quoted line give the plan's own year (its coverage
#: period or plan year, or a title such as "2026 Evidence of Coverage"),
#: not a revision or print date.
_PLAN_YEAR_WORDS = re.compile(
    r"plan\s+year|coverage\s+(?:period|year|begins|beginning|starts)"
    r"|(?:benefit|policy|contract)\s+(?:period|year)|calendar\s+year"
    r"|effective|begins|beginning"
    r"|evidence\s+of\s+coverage|certificate\s+of\s+coverage|summary\s+of\s+benefits"
    r"|benefits?\s+(?:booklet|guide|summary)|open\s+enrollment"
    r"|evidencia\s+de\s+cobertura|resumen\s+de\s+beneficios"
    r"|per[ií]odo\s+de\s+cobertura|a[ñn]o\s+del\s+plan|vigen(?:te|cia)",
    re.IGNORECASE,
)
#: A type's words right after "not" ("not an ERISA plan") deny it.
_NEGATED = re.compile(r"\bnot\s+(?:an?\s+|the\s+)?$", re.IGNORECASE)
#: Each model's answer, and all of them together, a second look included:
#: the reading's activity has 5 minutes
#: (workflows/shared_plan_documents.READ_TIMEOUT) and reading the file can
#: take a little over three with OCR, so the company step stays inside the
#: rest.
MODEL_TIMEOUT_SECONDS = 30.0
TOTAL_TIMEOUT_SECONDS = 70.0

SYSTEM_PROMPT = (
    "You read health plan documents and find the employer that sponsors the "
    "plan, and the plan's insurer, year and type. You answer with one JSON "
    "object and nothing else."
)

_PLAN_TYPES = "; ".join(
    f"{kind}: {label}"
    for kind, label in PolicyDocument.PLAN_CATEGORY_CHOICES
    if kind in _PLAN_TYPE_WORDS
)

ASK = (
    """Below are excerpts from a health plan document. Find the plan sponsor: the employer, company or organization that offers this plan to its employees or members. Also find the plan's insurer, plan year and plan type when the excerpts state them.

Rules:
- The sponsor is not the insurance company or claims administrator (for example Anthem, Aetna, Cigna, UnitedHealthcare, Blue Cross, Kaiser), not the plan's own name (such as "... Welfare Benefit Plan"), and not a person.
- An insurer's medical policy, a coverage booklet that names no employer, or a page of test text has no sponsor: answer null.
- "evidence" is one sentence or line copied word for word from the excerpts that names the sponsor.
- "ein" is the sponsor's Employer Identification Number (12-3456789) only when the excerpts give it as the sponsor's; otherwise null.
- "insurer" is the insurance company or claims administrator that runs the plan, never a person; "insurer_evidence" is the line copied word for word that names it.
- "plan_year" is the year the plan's coverage period or plan year begins, as a number (2026); "plan_year_evidence" is the line copied word for word that gives it.
- "plan_type" is one of these keys: """
    + _PLAN_TYPES
    + """; "plan_type_evidence" is the line copied word for word that shows it.
- When the excerpts don't state the insurer, plan year or plan type, answer null for it and its evidence.

Answer with exactly this JSON:
{"sponsor": "<company name>" or null, "ein": "<12-3456789>" or null, "evidence": "<copied line>" or null, "insurer": "<company name>" or null, "insurer_evidence": "<copied line>" or null, "plan_year": <year> or null, "plan_year_evidence": "<copied line>" or null, "plan_type": "<key>" or null, "plan_type_evidence": "<copied line>" or null}

Excerpts:
<<<
%s
>>>"""
)


class PlanFacts(NamedTuple):
    """The plan's insurer, plan year and type as our model read them, each
    checked against the text; empty where none passed. Named as the
    PolicyDocument fields they fill."""

    insurer_name: str = ""
    plan_year: Optional[int] = None
    plan_category: str = ""


class PlanReading(NamedTuple):
    """What our models read in a document (aread_plan)."""

    sponsor: Optional[Sponsor]
    #: False when no model could be asked or none gave a usable answer, so
    #: the fixed patterns should try; True when a model read it, even if it
    #: found no sponsor.
    answered: bool
    facts: PlanFacts = PlanFacts()


def excerpt(text: str) -> str:
    """The parts of ``text`` the model reads, in document order, joined by
    "..." where something was left out: the head and the tail always, then
    the passages around party words in between, as far as the budget goes."""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return "\n...\n".join(
        text[start:end].strip() for start, end in _excerpt_spans(text)
    )


def _excerpt_spans(text: str) -> list[tuple[int, int]]:
    """Where excerpt() reads a ``text`` longer than MAX_EXCERPT_CHARS, as
    sorted spans that don't overlap."""
    head_end, tail_start = HEAD_CHARS, len(text) - TAIL_CHARS
    room = MAX_EXCERPT_CHARS - HEAD_CHARS - TAIL_CHARS
    windows: list[list[int]] = []
    for match in _PARTY_WORDS.finditer(text, head_end, tail_start):
        start = max(head_end, match.start() - WINDOW_CHARS)
        end = min(tail_start, match.end() + WINDOW_CHARS)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    keep = [(0, head_end)]
    for start, end in windows:
        if room <= 0:
            break
        end = min(end, start + room)
        keep.append((start, end))
        room -= end - start
    keep.append((tail_start, len(text)))
    return keep


def second_excerpt(text: str) -> str:
    """For a ``text`` longer than excerpt() reads: the passages around party
    words that excerpt() left out, those around the sponsor's own words
    (sponsor, EIN) first, within SECOND_EXCERPT_CHARS, in document order;
    "" when there are none."""
    if len(text) <= MAX_EXCERPT_CHARS:
        return ""
    head_end, tail_start = HEAD_CHARS, len(text) - TAIL_CHARS
    taken = _excerpt_spans(text)
    matches = sorted(
        _PARTY_WORDS.finditer(text, head_end, tail_start),
        key=lambda m: (_SPONSOR_WORDS.fullmatch(m.group()) is None, m.start()),
    )
    new: list[tuple[int, int]] = []
    room = SECOND_EXCERPT_CHARS
    for match in matches:
        if room <= 0:
            break
        # The head and the tail are taken, so a span sits on either side.
        at = bisect.bisect_right(taken, (match.start(), len(text)))
        before, after = taken[at - 1], taken[at]
        if before[1] > match.start():
            continue
        start = max(before[1], match.start() - WINDOW_CHARS)
        end = min(after[0], match.end() + WINDOW_CHARS, start + room)
        taken.insert(at, (start, end))
        new.append((start, end))
        room -= end - start
    parts: list[list[int]] = []
    for start, end in sorted(new):
        if parts and start <= parts[-1][1]:
            parts[-1][1] = end
        else:
            parts.append([start, end])
    return "\n...\n".join(text[start:end].strip() for start, end in parts)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _letters(s: str) -> str:
    # Every letter and digit, in any script: none may go unchecked.
    return re.sub(r"[\W_]+", " ", s.casefold()).strip()


_PLAN_WORDS_AT_END = re.compile(
    r"\s+(?:health\s+(?:and|&)\s+)?(?:welfare\s+benefits?|employee\s+benefits?"
    r"|welfare|group\s+health|health\s+benefits?|medical)\s+plans?$",
    re.IGNORECASE,
)


def _parse(answer: str) -> Optional[dict]:
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class _NoSponsor(Exception):
    """The model said the document names no sponsor."""


def checked_sponsor(
    answer: str, text: str, insurers: frozenset[str]
) -> Optional[Sponsor]:
    """The sponsor in a model's ``answer`` once it passes every check
    against the document ``text``; None when it doesn't. ``insurers`` is
    plan_sponsors.known_insurers(). Raises _NoSponsor when the model
    answered that the document names none."""
    data = _parse(answer)
    if data is None:
        return None
    if "sponsor" not in data:
        # An answer without the field isn't an answer; only an explicit
        # null says the document names no sponsor.
        return None
    raw = data["sponsor"]
    if raw is None or (
        isinstance(raw, str) and raw.strip().lower() in ("", "null", "none")
    ):
        raise _NoSponsor()
    if not isinstance(raw, str):
        return None
    evidence = data.get("evidence")
    if not isinstance(evidence, str) or len(evidence.strip()) < 4:
        return None
    squashed = _squash(text)
    if _squash(evidence) not in squashed:
        return None
    name = good_name(_PLAN_WORDS_AT_END.sub("", raw.strip()).strip(" ,.;:-"), insurers)
    if not name:
        return None
    # The whole name must be in the line it quotes, punctuation aside, so
    # the line is about this company and not another that shares a word.
    if _words(name) not in _words(evidence):
        return None
    return Sponsor(
        name=name[:300],
        ein=_sponsors_ein(data.get("ein"), evidence, text),
        evidence=_kept_evidence(evidence),
        source=SOURCE_MODEL,
    )


def _words(s: str) -> str:
    """``s`` as space-padded words, so one name matches only whole words."""
    return f" {_letters(s)} "


def _sponsors_ein(raw_ein: object, evidence: str, text: str) -> str:
    """The EIN the model gave, when the document gives it as the sponsor's:
    in the quoted line, or on the lines just after it (as in "Plan Sponsor:
    ... / Employer Identification Number: ..."), before another party is
    named. An EIN elsewhere may be another party's, so it is dropped."""
    if not isinstance(raw_ein, str):
        return ""
    digits = re.sub(r"[^0-9]", "", raw_ein)
    if len(digits) != 9:
        return ""
    ein = re.compile(rf"(?<!\d){digits[:2]}-?{digits[2:]}(?!\d)")
    if ein.search(evidence):
        return f"{digits[:2]}-{digits[2:]}"
    # The quoted line where it sits in the document, spacing and case aside.
    line = re.compile(
        r"\s+".join(re.escape(word) for word in evidence.split()), re.IGNORECASE
    )
    for found in line.finditer(text):
        end = found.end() + EIN_WINDOW_CHARS
        other = _OTHER_PARTY.search(text, found.end(), end)
        if ein.search(text, found.end(), other.start() if other else end):
            return f"{digits[:2]}-{digits[2:]}"
    return ""


def _kept_evidence(evidence: str) -> str:
    """The quoted line as stored and shown to staff."""
    return _scrubbed(evidence)[:EVIDENCE_CHARS]


def _scrubbed(line: str) -> str:
    """A quoted line cleaned the way the stored text is: the model reads the
    text before personal details are taken out."""
    from fighthealthinsurance.plan_document_scrub import scrub

    return scrub(re.sub(r"\s+", " ", line).strip())


def checked_facts(answer: str, cleaned_text: str) -> PlanFacts:
    """The insurer, plan year and plan type in a model's ``answer``, each
    kept only when it passes its checks against ``cleaned_text``, the
    document as it is stored, personal details taken out
    (plan_documents_processing.clean): a quoted line must be there, so a
    detail the cleaning takes out never becomes a fact."""
    data = _parse(answer)
    if data is None:
        return PlanFacts()
    squashed = _squash(cleaned_text)
    return PlanFacts(
        insurer_name=_checked_insurer(data, squashed),
        plan_year=_checked_year(data, squashed),
        plan_category=_checked_plan_type(data, squashed),
    )


def _quoted_line(data: dict, key: str, squashed_text: str) -> str:
    """The line an answer quotes under ``key``, with personal details taken
    out, when it is word for word in the stored text; "" otherwise. The
    model reads the text before cleaning, so its quote is cleaned first."""
    line = data.get(key)
    if not isinstance(line, str) or len(line.strip()) < 4:
        return ""
    scrubbed = _scrubbed(line)
    if _squash(scrubbed) not in squashed_text:
        return ""
    return scrubbed


def _checked_insurer(data: dict, squashed_text: str) -> str:
    raw = data.get("insurer")
    if not isinstance(raw, str):
        return ""
    name = organization_name(raw)
    if not name:
        return ""
    # A name the cleaning takes out of its line was a person's detail.
    if _words(name) not in _words(
        _quoted_line(data, "insurer_evidence", squashed_text)
    ):
        return ""
    sponsor = data.get("sponsor")
    if isinstance(sponsor, str) and normalize_name(sponsor) == normalize_name(name):
        # A self-funded plan's employer is not its insurer.
        return ""
    return name[:200]


def _checked_year(data: dict, squashed_text: str) -> Optional[int]:
    raw = data.get("plan_year")
    if isinstance(raw, str) and re.fullmatch(r"[0-9]{4}", raw.strip()):
        year = int(raw.strip())
    elif isinstance(raw, int) and not isinstance(raw, bool):
        year = raw
    else:
        return None
    if not EARLIEST_PLAN_YEAR <= year <= timezone.now().year + 1:
        return None
    line = _quoted_line(data, "plan_year_evidence", squashed_text)
    # The year whole, not part of a longer number; a birth date is cleaned out.
    if not re.search(rf"(?<![0-9]){year}(?![0-9])", line):
        return None
    if not _PLAN_YEAR_WORDS.search(line):
        return None
    return year


def _checked_plan_type(data: dict, squashed_text: str) -> str:
    raw = data.get("plan_type")
    if not isinstance(raw, str):
        return ""
    kind = raw.strip().lower()
    words = _PLAN_TYPE_WORDS.get(kind)
    if words is None:
        return ""
    line = _quoted_line(data, "plan_type_evidence", squashed_text)
    if not any(not _NEGATED.search(line, 0, m.start()) for m in words.finditer(line)):
        return ""
    if any(denied.search(line) for denied in _PLAN_TYPE_DENIED.get(kind, ())):
        return ""
    return kind


async def aread_plan(text: str, cleaned_text: str) -> PlanReading:
    """What our models read in ``text``: the sponsor, whether a model
    answered (PlanReading.answered), and the plan's insurer, year and type,
    those three checked against ``cleaned_text`` (checked_facts).

    When a model finds no sponsor in a document longer than its excerpt, it
    reads the passages the excerpt left out (second_excerpt), once, if
    MODEL_TIMEOUT_SECONDS are still left of TOTAL_TIMEOUT_SECONDS. The
    insurer, year and type come from the first reading."""
    from channels.db import database_sync_to_async

    from fighthealthinsurance.plan_sponsors import known_insurers

    if not text or not text.strip():
        return PlanReading(None, True)
    insurers = await database_sync_to_async(known_insurers)()
    clock = asyncio.get_running_loop().time
    deadline = clock() + TOTAL_TIMEOUT_SECONDS
    first = await _aask(excerpt(text), text, insurers, deadline)
    if first is None:
        return PlanReading(None, False)
    sponsor, answer = first
    facts = checked_facts(answer, cleaned_text)
    if sponsor is None and deadline - clock() >= MODEL_TIMEOUT_SECONDS:
        more = second_excerpt(text)
        second = await _aask(more, text, insurers, deadline) if more else None
        if second is not None:
            sponsor = second[0]
    if sponsor is not None and normalize_name(sponsor.name) == normalize_name(
        facts.insurer_name
    ):
        # A self-funded plan's employer is not its insurer, by the sponsor's
        # name as checked, from either reading.
        facts = facts._replace(insurer_name="")
    return PlanReading(sponsor, True, facts)


async def _aask(
    part: str, text: str, insurers: frozenset[str], deadline: float
) -> Optional[tuple[Optional[Sponsor], str]]:
    """Our models' reading of ``part``, an excerpt of ``text``, by
    ``deadline`` on the event loop's clock: (the sponsor in the first answer
    that passes, or None when it says there is none; that answer). None
    when no model gave a usable answer in time."""
    from fighthealthinsurance.ml.ml_inference import infer_with_fallback

    found: dict[str, Sponsor] = {}

    def usable(answer: str) -> bool:
        try:
            sponsor = checked_sponsor(answer, text, insurers)
        except _NoSponsor:
            return True
        if sponsor is None:
            return False
        found["sponsor"] = sponsor
        return True

    try:
        answer = await asyncio.wait_for(
            infer_with_fallback(
                system_prompts=[SYSTEM_PROMPT],
                prompt=ASK % part,
                temperature=0.0,
                timeout=MODEL_TIMEOUT_SECONDS,
                label="plan_sponsor",
                validator=usable,
                task="entity_extraction",
            ),
            timeout=deadline - asyncio.get_running_loop().time(),
        )
    except asyncio.TimeoutError:
        logger.warning("Plan sponsor model read ran out of time")
        return None
    except Exception as e:
        # The class only: a model error's message can quote the document.
        logger.warning(f"Plan sponsor model read failed: {type(e).__name__}")
        return None
    if answer is None:
        return None
    return found.get("sponsor"), answer

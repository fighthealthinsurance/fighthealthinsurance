"""Triage a denial letter with TypeSafe's System One: what kind of denial it
is, who regulates the plan, whether it is pre-service or urgent, and the
internal-appeal deadline the letter states.

Why: intake gets the denial category and the regulator from regexes and knows
nothing about urgency or the deadline, the fact most likely to lose someone an
appeal they would have won. System One answers typed questions over a document
deterministically for a fraction of a cent, which is exactly this shape of
problem.

How the deadline works, because System One has no free-text primitive. The
letter is scanned for candidate dates ("October 15, 2026", "10/15/2026",
"2026-10-15") and day windows ("within 180 days of this notice"), in document
order, each becoming an option in a ``choice`` question whose description is
the sentence it sits in. The question asks for the INTERNAL appeal deadline
specifically and offers "none of these", because a letter routinely carries
several deadlines (external review, records request, corrected claim) and a
forced pick between them would be worse than no answer. A day window only
resolves to a date when it is in calendar days AND anchored to this notice
("within 180 days of this letter"), and only once we hold the denial date;
"business days", "after receipt" and "from the date of service" stay
unresolved on purpose, with the window kept as text for a person to read.

What a reader gets. Nothing, yet. The triage is stored with the model's
confidence and a hash of the text it was computed from, and shown to staff
in the admin. ``deadline_to_show`` and ``deadline_sentence`` are the
patient-facing rule (confident, still ahead, computed from the CURRENT text)
but are not wired to any patient-facing surface until the extraction has been
checked against a representative set of real letters. A confidently wrong
date can cost someone an appeal; a missing one cannot.

Data protection: the denial text only, never the form fields, and only when
the user allowed external models (``denial.use_external``). Inert until both
``TYPESAFE_API_KEY`` and ``TYPESAFE_DENIAL_TRIAGE_ENABLED`` are set; fails
closed; never logs the text.
"""

import asyncio
import dataclasses
import datetime
import hashlib
import re
import typing

from django.conf import settings
from loguru import logger

from fighthealthinsurance.ml import typesafe

MODEL = typesafe.DEFAULT_MODEL
# Bump when the questions or the candidate rules change: a stored triage
# from an older rubric is not "current" and gets redone. The model half is
# whatever TypeSafe reports (the alias when it reports nothing); see
# letter_quality.scorer_for for the same limit, accepted.
RUBRIC_VERSION = 1
_RUBRIC_SUFFIX = f"/rubric-{RUBRIC_VERSION}"
SOURCE = f"typesafe/{MODEL}{_RUBRIC_SUFFIX}"


def source_for(payload: typing.Any) -> str:
    answered = payload.get("model") if isinstance(payload, dict) else None
    model = str(answered).strip() if answered else MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", model):
        model = MODEL
    return f"typesafe/{model}{_RUBRIC_SUFFIX}"  # <= 66 chars; column is 80


def same_rubric(source: typing.Optional[str]) -> bool:
    return (
        bool(source)
        and str(source).startswith("typesafe/")
        and str(source).endswith(_RUBRIC_SUFFIX)
    )


# A deadline is only ever SHOWN to a reader at or above this confidence.
DEADLINE_CONFIDENCE_TO_SHOW = 0.7

MAX_DATE_CANDIDATES = 8
NONE_LABEL = "none_of_these"

CATEGORIES: dict[str, str] = {
    "medical_necessity": (
        "The insurer says the service is not medically necessary or does not "
        "meet its clinical criteria or guidelines"
    ),
    "prior_authorization": (
        "No prior authorization, pre-certification or referral was obtained, "
        "or the request for one was denied"
    ),
    "out_of_network": "The provider, facility or pharmacy is out of the plan's network",
    "not_a_covered_benefit": (
        "The plan excludes the service, drug or device, or the benefit is "
        "exhausted, capped or limited"
    ),
    "experimental_investigational": (
        "The service is called experimental, investigational or unproven"
    ),
    "coding_or_billing": (
        "A coding, billing, documentation, timely-filing or duplicate-claim problem"
    ),
    "eligibility": (
        "The patient was not eligible or enrolled on the date of service, or "
        "coverage had ended or premiums were unpaid"
    ),
    "other": "None of the above fits the stated reason",
}

REGULATION: dict[str, str] = {
    "employer_plan": (
        "An employer-sponsored group health plan (ERISA), including self-funded "
        "plans: an employer or group name, a plan administrator, a reference to "
        "ERISA or the Department of Labor"
    ),
    "state_regulated": (
        "An individual, marketplace or fully insured plan regulated by a state "
        "insurance department"
    ),
    "medicare": "Medicare or a Medicare Advantage plan",
    "medicaid": "Medicaid, CHIP or a state managed-care Medicaid plan",
    "unknown": "The letter does not say enough to tell",
}

PRE_SERVICE_QUESTION = (
    "The denial is for a service the patient has not yet received (a prior "
    "authorization or pre-service request), not a claim for care already provided"
)
URGENT_QUESTION = (
    "The letter indicates an expedited or urgent review, or that delay could "
    "seriously jeopardize the patient's health or ability to regain function"
)
DEADLINE_QUESTION = (
    "Which of these is the deadline by which the patient must file an INTERNAL "
    "appeal with the insurer? Not an external or independent review deadline, "
    "not a records request, not a corrected-claim window, not a date of "
    "service or a date the letter was written."
)
NONE_DESCRIPTION = (
    "None of these is the internal appeal deadline, or the letter gives more "
    "than one deadline for different actions and it is not clear which applies"
)

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
# Four-digit years only ("10/15/26" is ambiguous), and not glued to a
# claim-number style run of digits, slashes or dashes on either side.
_ABSOLUTE = re.compile(
    rf"(?<![\w/-])(?:"
    rf"\d{{1,2}}/\d{{1,2}}/\d{{4}}"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
    rf")(?![\w/-])",
    re.IGNORECASE,
)
_RELATIVE = re.compile(
    r"\b(?:within|no later than|not later than|by)?\s*(\d{1,3})\s+(calendar\s+|business\s+|working\s+)?days\b",
    re.IGNORECASE,
)
# What a day window may be counted FROM for us to resolve it: this letter,
# and the anchor must be the very next words after the window ("within 180
# days of this notice"), not something found further along the sentence
# ("within 30 days after receipt. Keep a copy of this notice"). A possessive
# or a receipt after the anchor ("of this letter's receipt") is not the
# letter date either.
_ANCHORED_TO_NOTICE = re.compile(
    r"\s*,?\s*(?:of|from|after|following)\s+(?:the\s+date\s+(?:of|on)\s+)?(?:this|the)\s+"
    r"(?:notice|letter|determination|decision|denial|notification|adverse\s+benefit\s+determination)"
    r"(?![\w'\u2019])",
    re.IGNORECASE,
)
# After the anchor phrase only a clause end or a harmless continuation may
# follow. Anything else ("has been received", "being received", "is
# delivered", "becoming final", "'s receipt") shifts the start date to an
# event we do not know, and the window stays unresolved.
_SAFE_CONTINUATION = re.compile(
    r"^\s*(?:$|[.;:)]|,?\s*(?:to|or|and|in\s+order|if|unless|at|by)\b)",
    re.IGNORECASE,
)
# ...and whatever follows in the same clause must not move the start date
# ("of this notice, and the period begins when you receive it").
_MOVES_THE_START = re.compile(
    r"receiv|deliver|postmark|mail|final|begin|start|commenc", re.IGNORECASE
)
_CLAUSE_END = re.compile(r"[.;:)\n]")


def _continuation_is_safe(rest: str) -> bool:
    if not _SAFE_CONTINUATION.match(rest):
        return False
    clause = _CLAUSE_END.split(rest, 1)[0][:160]
    return not _MOVES_THE_START.search(clause)


_ANCHOR_WINDOW_CHARS = 90
_SENTENCE_CAP = 300


@dataclasses.dataclass(frozen=True)
class DateCandidate:
    label: str  # the option label sent to TypeSafe
    snippet: str  # its description: the sentence it sits in
    resolves_to: typing.Optional[datetime.date]
    position: int
    days: typing.Optional[int] = None
    anchored: bool = False  # a day window counted from this notice, in calendar days


def _parse_absolute(raw: str) -> typing.Optional[datetime.date]:
    from dateutil import parser

    try:
        return parser.parse(raw, dayfirst=False, fuzzy=False).date()
    except (ValueError, OverflowError):
        return None


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence (or the line, in a list layout) containing [start, end),
    whitespace collapsed, capped."""
    left = max(
        text.rfind(".", 0, start), text.rfind("\n", 0, start), text.rfind(";", 0, start)
    )
    right_candidates = [
        i
        for i in (text.find(".", end), text.find("\n", end), text.find(";", end))
        if i != -1
    ]
    right = min(right_candidates) if right_candidates else len(text)
    sentence = " ".join(text[left + 1 : right + 1].split())
    if len(sentence) > _SENTENCE_CAP:
        # Keep the match visible: trim symmetrically around it.
        lo = max(0, start - left - 1 - _SENTENCE_CAP // 2)
        sentence = " ".join(text[left + 1 + lo : left + 1 + lo + _SENTENCE_CAP].split())
    return sentence


def window_label(days: int, unit: str, anchored: bool) -> str:
    if unit == "business":
        return f"{days} business days"
    return f"{days} days from notice" if anchored else f"{days} days"


_WINDOW_LABEL = re.compile(r"^(\d{1,3}) days from notice$")


def resolve_window(
    label: typing.Optional[str], denial_date: typing.Optional[datetime.date]
) -> typing.Optional[datetime.date]:
    """A stored day window becomes a date only if it was anchored to this
    notice and we now hold the denial date."""
    if not label or denial_date is None:
        return None
    match = _WINDOW_LABEL.match(label)
    if not match:
        return None
    return denial_date + datetime.timedelta(days=int(match.group(1)))


def date_candidates(
    text: typing.Optional[str],
    denial_date: typing.Optional[datetime.date],
    *,
    limit: int = MAX_DATE_CANDIDATES,
) -> list[DateCandidate]:
    """Every date or day window in the letter, in DOCUMENT order, deduped.

    When there are more than ``limit``, the ones whose sentence mentions an
    appeal are kept first: a letter can list many dates of service and one
    appeal window, and the window is the one we are here for.
    """
    text = text or ""
    found: list[DateCandidate] = []
    seen: dict[str, int] = {}  # label -> index in found

    def keep(candidate: DateCandidate) -> None:
        # One option per label. When the same date appears twice, the
        # occurrence whose sentence talks about an appeal is the one the
        # model needs to see; a service date repeated as the deadline must
        # not be hidden behind its first, irrelevant mention.
        index = seen.get(candidate.label)
        if index is None:
            seen[candidate.label] = len(found)
            found.append(candidate)
            return
        current = found[index]
        if (
            "appeal" in candidate.snippet.lower()
            and "appeal" not in current.snippet.lower()
        ):
            found[index] = candidate

    for match in _ABSOLUTE.finditer(text):
        resolved = _parse_absolute(match.group(0))
        if resolved is None:
            continue
        keep(
            DateCandidate(
                resolved.isoformat(),
                _sentence(text, *match.span()),
                resolved,
                match.start(),
            )
        )

    for match in _RELATIVE.finditer(text):
        days = int(match.group(1))
        if days <= 0:
            continue
        unit_raw = (match.group(2) or "").strip().lower()
        unit = "business" if unit_raw in ("business", "working") else "calendar"
        tail = text[match.end() : match.end() + _ANCHOR_WINDOW_CHARS]
        anchor = _ANCHORED_TO_NOTICE.match(tail) if unit == "calendar" else None
        anchored = anchor is not None and _continuation_is_safe(tail[anchor.end() :])
        label = window_label(days, unit, anchored)
        keep(
            DateCandidate(
                label,
                _sentence(text, *match.span()),
                resolve_window(label, denial_date),
                match.start(),
                days=days,
                anchored=anchored,
            )
        )

    found.sort(key=lambda c: c.position)
    if len(found) > limit:
        appeal_first = [c for c in found if "appeal" in c.snippet.lower()]
        rest = [c for c in found if "appeal" not in c.snippet.lower()]
        found = (appeal_first + rest)[:limit]
        found.sort(key=lambda c: c.position)
    return found


def build_questions(
    candidates: list[DateCandidate],
) -> dict[str, dict[str, typing.Any]]:
    questions: dict[str, dict[str, typing.Any]] = {
        "category": {
            "type": "choice",
            "instructions": "What is the insurer's stated reason for this denial?",
            "criteria": dict(CATEGORIES),
        },
        "regulation": {
            "type": "choice",
            "instructions": "What kind of plan issued this denial?",
            "criteria": dict(REGULATION),
        },
        "pre_service": {"type": "noul", "instructions": PRE_SERVICE_QUESTION},
        "urgent": {"type": "noul", "instructions": URGENT_QUESTION},
    }
    if candidates:
        options = {c.label: f"{c.label}: {c.snippet}" for c in candidates}
        options[NONE_LABEL] = NONE_DESCRIPTION
        questions["deadline"] = {
            "type": "choice",
            "instructions": DEADLINE_QUESTION,
            "criteria": options,
        }
    return questions


@dataclasses.dataclass(frozen=True)
class Triage:
    category: str
    category_confidence: float
    regulation: str
    regulation_confidence: float
    pre_service: float  # probability
    urgent: float  # probability
    deadline: typing.Optional[datetime.date]
    deadline_label: typing.Optional[str]
    deadline_confidence: typing.Optional[float]
    input_tokens: int
    source: str = SOURCE


class TriageError(Exception):
    """A response we could not turn into a Triage."""


outcomes: dict[str, int] = {"triaged": 0, "failed": 0, "skipped": 0}


def _count(outcome: str) -> None:
    outcomes[outcome] = outcomes.get(outcome, 0) + 1


def enabled() -> bool:
    return typesafe.configured() and bool(
        getattr(settings, "TYPESAFE_DENIAL_TRIAGE_ENABLED", False)
    )


def text_hash(text: typing.Optional[str]) -> str:
    """Identifies the text a triage was computed from, so a result that
    arrives after the letter was replaced is recognisable as stale."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _unit(value: typing.Any, what: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise TriageError(f"{what} out of range")
    return number


def _choice(
    answers: typing.Any, name: str, allowed: typing.Iterable[str]
) -> tuple[str, float]:
    answer = answers[name]
    label = str(answer["choice"])
    if label not in set(allowed):
        raise TriageError(f"{name} chose an unknown option")
    return label, _unit(answer.get("confidence", 0.0), f"{name} confidence")


def parse(payload: typing.Any, candidates: list[DateCandidate]) -> Triage:
    """Strict: an unknown label or a missing answer means the API changed
    under us, and a silently wrong category would steer a letter."""
    try:
        answers = payload["answers"]
        category, category_conf = _choice(answers, "category", CATEGORIES)
        regulation, regulation_conf = _choice(answers, "regulation", REGULATION)
        pre_service = _unit(answers["pre_service"]["noul"], "pre_service")
        urgent = _unit(answers["urgent"]["noul"], "urgent")
        deadline: typing.Optional[datetime.date] = None
        deadline_label: typing.Optional[str] = None
        deadline_conf: typing.Optional[float] = None
        if candidates:
            by_label = {c.label: c for c in candidates}
            label, deadline_conf = _choice(
                answers, "deadline", list(by_label) + [NONE_LABEL]
            )
            if label != NONE_LABEL:
                deadline_label = label
                deadline = by_label[label].resolves_to
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
    except TriageError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise TriageError(f"unexpected response shape: {type(e).__name__}") from e
    return Triage(
        category=category,
        category_confidence=category_conf,
        regulation=regulation,
        regulation_confidence=regulation_conf,
        pre_service=pre_service,
        urgent=urgent,
        deadline=deadline,
        deadline_label=deadline_label,
        deadline_confidence=deadline_conf,
        input_tokens=input_tokens,
        source=source_for(payload),
    )


async def _post(
    document: str, questions: dict[str, typing.Any], timeout_seconds: float
) -> typing.Any:
    # Kept as a seam: tests stub this one function to stay off the network.
    return await typesafe.ask(
        document, questions, timeout_seconds=timeout_seconds, model=MODEL
    )


async def triage(
    denial_text: typing.Optional[str],
    denial_date: typing.Optional[datetime.date],
    *,
    timeout_seconds: typing.Optional[float] = None,
) -> typing.Optional[Triage]:
    """Triage one denial, or return None. Never raises, never logs the text."""
    if not enabled():
        _count("skipped")
        return None
    text = (denial_text or "").strip()
    if not text:
        _count("skipped")
        return None
    timeout = timeout_seconds or float(
        getattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 20)
    )
    candidates = date_candidates(text, denial_date)
    try:
        payload = await _post(text, build_questions(candidates), timeout)
        result = parse(payload, candidates)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _count("failed")
        logger.warning(f"denial triage unavailable: {type(e).__name__}: {e}")
        return None
    _count("triaged")
    return result


TRIAGE_COLUMNS = (
    "triage_category",
    "triage_category_confidence",
    "triage_regulation",
    "triage_regulation_confidence",
    "triage_pre_service",
    "triage_urgent",
    "appeal_deadline",
    "appeal_deadline_label",
    "appeal_deadline_confidence",
    "triage_source",
    "triage_text_hash",
    "triaged_at",
)


def row_values(
    result: Triage, now: datetime.datetime, denial_text: typing.Optional[str]
) -> dict[str, typing.Any]:
    """The Denial columns a triage writes; one place so the hook and the
    tests agree on them. Carries the hash of the text it came from."""
    return {
        "triage_category": result.category,
        "triage_category_confidence": result.category_confidence,
        "triage_regulation": result.regulation,
        "triage_regulation_confidence": result.regulation_confidence,
        "triage_pre_service": result.pre_service,
        "triage_urgent": result.urgent,
        "appeal_deadline": result.deadline,
        "appeal_deadline_label": result.deadline_label,
        "appeal_deadline_confidence": result.deadline_confidence,
        "triage_source": result.source,
        "triage_text_hash": text_hash(denial_text),
        "triaged_at": now,
    }


def cleared_values() -> dict[str, typing.Any]:
    """What to write when the letter changes: every triage column back to
    null, so nothing computed from the old text can be read as current."""
    return {column: None for column in TRIAGE_COLUMNS}


def is_anchored_window(label: typing.Optional[str]) -> bool:
    return bool(label) and bool(_WINDOW_LABEL.match(str(label)))


def is_current(denial: typing.Any) -> bool:
    """Whether the stored triage was computed from the denial's current text,
    under the current rubric."""
    stored = getattr(denial, "triage_text_hash", None)
    return (
        bool(stored)
        and stored == text_hash(getattr(denial, "denial_text", None))
        and same_rubric(getattr(denial, "triage_source", None))
    )


def deadline_to_show(
    denial: typing.Any, today: datetime.date
) -> typing.Optional[datetime.date]:
    """The deadline a reader may be told about, or None.

    Confidently identified, computed from the current text, and strictly
    after today: a deadline that is today (or earlier) would read as "you
    already lost", which the letter may not even say. Not wired to any
    patient-facing surface yet; see the module docstring.
    """
    deadline = getattr(denial, "appeal_deadline", None)
    confidence = getattr(denial, "appeal_deadline_confidence", None)
    if not isinstance(deadline, datetime.date):
        return None
    if (
        not isinstance(confidence, (int, float))
        or confidence < DEADLINE_CONFIDENCE_TO_SHOW
    ):
        return None
    if not is_current(denial):
        return None
    if deadline <= today:
        return None
    return deadline


def deadline_sentence(denial: typing.Any, today: datetime.date) -> str:
    """One hedged sentence, or an empty string."""
    deadline = deadline_to_show(denial, today)
    if deadline is None:
        return ""
    return (
        f"Your denial letter appears to say appeals are due by "
        f"{deadline.strftime('%B %-d, %Y')}. Check the letter to be sure."
    )

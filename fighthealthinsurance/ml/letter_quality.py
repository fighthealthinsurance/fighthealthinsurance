"""Score appeal drafts for ORDERING with TypeSafe's System One API.

Why this exists: the router fans a denial out to up to nine backends and every
draft that comes back lands on the page in arrival order, so the reader gets a
wall of letters with nothing to tell the strong one from the weak one. This
module puts a number on each draft so the client can show the best first, and
so the staff dashboard can watch draft quality per backend without waiting
weeks for appeal outcomes to trickle in.

What the number is, and is not. The four questions below are the ones our
model evaluation validated: across 12,536 letters the composite correlated
0.74 with the judge ensemble per letter (higher than the judges agreed with
each other) and ranked backends at Spearman 0.90. It is a criteria score --
does the letter engage the stated denial reason, argue medical necessity for
THIS case, invent nothing, read as send-ready. It is NOT a probability that
the appeal succeeds: nobody has calibrated it against real outcomes, and
insurers overturn roughly a third to a half of internal appeals, so any
honest "chance of success" would read as discouraging for nearly everyone.
Never surface it as one. The client copy says "ordered by how well each
letter addresses your denial", and that is the whole claim.

``no_invented_facts`` doubles as a grounding signal. Our evaluation's biggest
finding was fabrication, and this question caught it at precision 0.69 /
recall 0.51 for about a thousandth of a cent per letter: good enough to
DEMOTE a draft, not to delete one. Treat a low grounding score that way.

Data protection, stated exactly. TypeSafe receives the denial text and the
draft: the same two things the external language models that WROTE the draft
already received and produced, under the same consent (``denial.use_external``).
It never receives the form fields. Before sending, ``Redactor`` strips the
identifiers ``scoring_redactions`` (common_view_logic) reads from the profile
fields we hold -- patient and professional names, user first/last names,
emails, phone and fax numbers, NPI, claim and plan ids, employer, the
professional's practice address, and the address lines and phone on each
user's contact record -- and, in the text itself, every email address (Unicode letters allowed,
alphabetic or punycode top-level domain) and every North American style
phone number, each distinct value getting its own stable token so the
"invented facts" comparison still works. Other phone formats are not
recognised. That is a reduction, NOT de-identification: a
dependent's name, a member id, a date of birth or an address that exists only
in the free text of the letter is not something we hold, and it goes
through; a one-word name is only caught in its Capitalised and ALL-CAPS
spellings. A draft is not clean by construction either -- the
generation prompt asks the model to "include and fill in" the patient's and
professional's details. So the gate for turning this on is contractual, not
technical: TypeSafe must be under the same data terms as the other providers
and named in the privacy policy first. If collecting the identifiers fails,
scoring is off for the run. The module is inert until BOTH
``TYPESAFE_API_KEY`` and ``TYPESAFE_LETTER_RANKING_ENABLED`` are set, and it
fails closed: any error, timeout or malformed answer means "no score", never a
broken stream and never a logged document.
"""

import asyncio
import dataclasses
import re
import typing

from django.conf import settings
from loguru import logger

from fighthealthinsurance.ml import typesafe

# Recorded on every scored row, and every aggregate filters on it. Two things
# move the scale: TypeSafe's "speed_latest" is an alias they can repoint, and
# the questions below are our half of the rubric. Bump RUBRIC_VERSION whenever
# the questions change; either change starts a fresh series on the dashboard
# instead of averaging two scales into one line called "drift".
MODEL = typesafe.DEFAULT_MODEL
RUBRIC_VERSION = 1
_RUBRIC_SUFFIX = f"/rubric-{RUBRIC_VERSION}"
SCORER = f"typesafe/{MODEL}{_RUBRIC_SUFFIX}"


def scorer_for(payload: typing.Any) -> str:
    """The provenance string for a row: the model TypeSafe says answered
    plus our rubric version.

    Known limit, accepted: System One only exposes the moving alias
    ("speed_latest") and, in some responses, the resolved model it used. When
    the response names none, the alias is recorded, and a repoint that hides
    behind the alias is invisible to us. The rubric half is ours and exact;
    the model half is as good as the provider makes it."""
    answered = None
    if isinstance(payload, dict):
        answered = payload.get("model")
    model = str(answered).strip() if answered else MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", model):
        model = MODEL  # never let an odd answer forge the provenance format
    return f"typesafe/{model}{_RUBRIC_SUFFIX}"


_SCORER_RE = re.compile(r"^typesafe/([A-Za-z0-9._-]{1,48})/rubric-(\d{1,4})$")


def same_rubric(scorer: typing.Optional[str]) -> bool:
    """Whether a stored score was produced by TypeSafe under the CURRENT
    rubric, and so may be reused or ranked against fresh scores. The whole
    string must be ours: a hand-imported "manual/rubric-1" is not."""
    match = _SCORER_RE.match(str(scorer or ""))
    return match is not None and int(match.group(2)) == RUBRIC_VERSION


DOCUMENT_CHAR_CAP = typesafe.DOCUMENT_CHAR_CAP
# Longer than this and we do not score at all: cutting raw text could split
# an identifier, and every scan is bounded by this. It is eight times what
# can be sent, so a real letter never comes near it.
RAW_CHAR_BOUND = 8 * DOCUMENT_CHAR_CAP

# Verbatim from the validated evaluation pass. Changing the wording changes
# the scale, so a change here must also bump SCORER.
QUESTIONS: dict[str, dict[str, typing.Any]] = {
    "cites_denial": {
        "type": "score",
        "instructions": "How directly the appeal letter engages the insurer's stated denial reason",
        "criteria": [
            "Never engages with the stated reason",
            "Vague or generic engagement",
            "Quotes or clearly restates the reason and rebuts it",
        ],
    },
    "medical_necessity": {
        "type": "score",
        "instructions": "How specific the medical-necessity argument is to this patient's case",
        "criteria": [
            "Absent",
            "Boilerplate",
            "Specific to the case, points to guidelines or plan terms where sensible",
        ],
    },
    "no_invented_facts": {
        "type": "score",
        "instructions": (
            "Whether the letter invents facts (dates, dollar amounts, policy numbers, "
            "providers, results, history) not present in the request; placeholders "
            "like [DATE] are fine"
        ),
        "criteria": [
            "Fabricates facts",
            "Minor embellishment that would not mislead",
            "Clean",
        ],
    },
    "tone_and_form": {
        "type": "score",
        "instructions": (
            "Whether this reads as a send-ready appeal letter; trailing advice or "
            "bracketed slots for substantive facts cap it below ready"
        ),
        "criteria": ["Not a letter or rambles", "Rough but usable", "Ready to send"],
    },
}
SCORE_QUESTIONS: tuple[str, ...] = tuple(QUESTIONS)
# Three criteria -> levels 0, 1, 2. System One returns the probability-weighted
# EXPECTED level, so an answer is a float like 1.3, not an integer: keep it.
MAX_PER_QUESTION = 2.0
GROUNDING_QUESTION = "no_invented_facts"

# Ungrounded drafts sort below everything that scored at or above this.
GROUNDING_DEMOTE_BELOW = 1


@dataclasses.dataclass(frozen=True)
class LetterScore:
    quality: float  # 0..1 composite of the four questions
    grounding: float  # 0..2, the no_invented_facts answer on its own
    scores: dict[str, float]
    input_tokens: int
    scorer: str = SCORER


class LetterScoringError(Exception):
    """A response we could not turn into a score."""


# Process-local request outcomes, exported by letter_quality_metrics.
outcomes: dict[str, int] = {"scored": 0, "failed": 0, "skipped": 0}


def _count(outcome: str) -> None:
    outcomes[outcome] = outcomes.get(outcome, 0) + 1


def enabled() -> bool:
    return bool(getattr(settings, "TYPESAFE_API_KEY", None)) and bool(
        getattr(settings, "TYPESAFE_LETTER_RANKING_ENABLED", False)
    )


# (identifier as we hold it, category). A category may carry an entity
# suffix, "PROFESSIONAL#primary": every alias of that one person (legal
# name, display name, first name, last name) then shares ONE token, so a
# draft that says "Sam Smith MD" where the denial said "Sam Smith" is not
# read as introducing a second provider.
Redaction = tuple[str, str]

# Bounded, linear scans, never a backtracking regex: the denial text is
# unbounded user input and this runs on the event loop.
_EMAIL_LOCAL_EXTRA = set("!#$%&'*+-/=?^_`{|}~.")  # RFC 5322 atext, plus the dot
_EMAIL_DOMAIN_EXTRA = set(".-")
_EMAIL_LOCAL_MAX = 64
_EMAIL_DOMAIN_MAX = 253
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)
# A one-word name can be two letters ("Li"); anything else that short would
# hit ordinary text.
MIN_NAME_CHARS = 2
MIN_IDENTIFIER_CHARS = 3
_NAME_CATEGORIES = {"PATIENT", "PROFESSIONAL"}
_UNKNOWN = "UNKNOWN"


def _is_local_char(ch: str) -> bool:
    return ch.isalnum() or ch in _EMAIL_LOCAL_EXTRA


def _is_domain_char(ch: str) -> bool:
    return ch.isalnum() or ch in _EMAIL_DOMAIN_EXTRA


def _email_spans(text: str) -> list[tuple[int, int]]:
    """Linear email finder: from each '@', walk left over local-part characters
    and right over domain characters (both length-capped, Unicode letters
    allowed), and require a dot followed by at least two letters."""
    spans: list[tuple[int, int]] = []
    last_end = 0
    for at in range(len(text)):
        if text[at] != "@" or at < last_end:
            continue
        start = at
        while (
            start > last_end
            and at - start < _EMAIL_LOCAL_MAX
            and _is_local_char(text[start - 1])
        ):
            start -= 1
        if start == at:
            continue
        end = at + 1
        while (
            end < len(text)
            and end - at - 1 < _EMAIL_DOMAIN_MAX
            and _is_domain_char(text[end])
        ):
            end += 1
        domain = text[at + 1 : end].rstrip(".-")
        end = at + 1 + len(domain)
        dot = domain.rfind(".")
        tld = domain[dot + 1 :]
        if (
            dot <= 0
            or len(tld) < 2
            or not (tld.isalpha() or tld.lower().startswith("xn--"))
        ):
            continue
        spans.append((start, end))
        last_end = end
    return spans


def _phone_key(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-10:] if len(digits) > 10 else digits


# (start, end, category, matched text, generic?)
_Span = tuple[int, int, str, str, bool]


class Redactor:
    """Replace identifiers with stable, DISTINCT tokens, in ONE pass.

    Every candidate span -- known identifiers, emails, phone-shaped values --
    is found on the original text; overlaps are settled by "longest wins"
    (a stored plan id that contains a phone number beats the phone, an email
    that contains a first name beats the name), and the output is assembled
    from the surviving spans. No pattern ever sees replaced text, so a token
    can never be re-matched and no fence characters are needed.

    Rules that matter for the score, not just for privacy:

    * Every distinct value gets its own numbered token ("[PHONE_1]",
      "[PHONE_2]"), shared across the denial and the draft. If both collapsed
      to "[PHONE]" a draft that invented a phone number would look grounded.
      Phone and fax values share one namespace keyed by digits, so
      "(415) 555-0100" and "415-555-0100" are one token.
    * A one-word name is matched only in its Capitalised and ALL-CAPS
      spellings (never lowercase, however the profile stores it), so a
      patient called Will or May does not blank every "will" and "may" in
      the letter; multi-word names, ids and emails are matched
      case-insensitively, whole-word.
    """

    def __init__(self, identifiers: typing.Iterable[Redaction] = ()):
        self._tokens: dict[str, str] = {}  # category:key -> token
        self._counts: dict[str, int] = {}
        self._known: list[tuple[re.Pattern[str], str]] = []
        seen: set[tuple[str, str]] = set()
        for value, category in identifiers:
            text = str(value).strip() if value is not None else ""
            if not text or text.upper() == _UNKNOWN:
                continue
            category = "PHONE" if category == "FAX" else category
            is_name = category.partition("#")[0] in _NAME_CATEGORIES
            floor = MIN_NAME_CHARS if is_name else MIN_IDENTIFIER_CHARS
            if len(text) < floor or (text, category) in seen:
                continue
            seen.add((text, category))
            if is_name and " " not in text:
                spellings = {text.capitalize(), text.upper()}
                if text[0].isupper():
                    spellings.add(text)
                alternatives = "|".join(re.escape(v) for v in sorted(spellings))
                pattern = re.compile(r"(?<!\w)(?:" + alternatives + r")(?!\w)")
            else:
                pattern = re.compile(
                    r"(?<!\w)" + re.escape(text) + r"(?!\w)", re.IGNORECASE
                )
            self._known.append((pattern, category))

    def _token(self, value: str, category: str) -> str:
        display, _, entity = category.partition("#")
        if entity:
            key = f"{display}#{entity}"  # one token per person, whatever the alias
        elif display == "PHONE":
            key = f"PHONE:{_phone_key(value)}"
        else:
            key = f"{display}:{value.lower()}"
        if key not in self._tokens:
            self._counts[display] = self._counts.get(display, 0) + 1
            self._tokens[key] = f"[{display}_{self._counts[display]}]"
        return self._tokens[key]

    def _spans(self, text: str) -> list[_Span]:
        """Non-overlapping spans covering EVERY character any candidate
        covered.

        Two partially overlapping identifiers ("Alice Smith" and "Smith
        Jones" on "Alice Smith Jones") become one span over their union, so
        no fragment of either can leave; the union takes the category of its
        longest member (ties: known before generic, then category name). A
        sorted sweep, so it is O(n log n) in the number of candidates.
        """
        found: list[_Span] = []
        for start, end in _email_spans(text):
            found.append((start, end, "EMAIL", text[start:end], True))
        for match in _PHONE_RE.finditer(text):
            found.append((match.start(), match.end(), "PHONE", match.group(0), True))
        for pattern, category in self._known:
            for match in pattern.finditer(text):
                found.append(
                    (match.start(), match.end(), category, match.group(0), False)
                )
        found.sort(key=lambda span: (span[0], -span[1]))
        merged: list[_Span] = []
        group: list[_Span] = []
        group_end = -1
        for span in found:
            if group and span[0] >= group_end:
                merged.append(self._union(text, group))
                group = []
            group.append(span)
            group_end = max(group_end, span[1])
        if group:
            merged.append(self._union(text, group))
        return merged

    @staticmethod
    def _union(text: str, group: list[_Span]) -> _Span:
        if len(group) == 1:
            return group[0]
        start = min(span[0] for span in group)
        end = max(span[1] for span in group)
        longest = min(
            group, key=lambda span: (-(span[1] - span[0]), span[4], span[2], span[0])
        )
        return (start, end, longest[2], text[start:end], longest[4])

    def redact(self, text: typing.Optional[str]) -> str:
        source = text or ""
        pieces: list[str] = []
        cursor = 0
        for start, end, category, value, _generic in self._spans(source):
            pieces.append(source[cursor:start])
            pieces.append(self._token(value, category))
            cursor = end
        pieces.append(source[cursor:])
        return "".join(pieces)


def redact(text: typing.Optional[str], identifiers: typing.Iterable[Redaction]) -> str:
    """One-shot convenience over Redactor (tokens numbered within this call)."""
    return Redactor(identifiers).redact(text)


def _cut(text: str, limit: int) -> str:
    """Truncate without splitting a token: a "[PAT" at the end would look
    like an invented word to the scorer."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    open_at = cut.rfind("[")
    if open_at != -1 and cut.rfind("]") < open_at:
        cut = cut[:open_at]
    return cut


def build_document(
    denial_text: typing.Optional[str],
    letter_text: str,
    identifiers: typing.Iterable[Redaction] = (),
) -> str:
    """The letter always fits; the denial gives way first. Both redacted.

    A denial is usually reason-first, so when it must be cut it is cut from
    the end. A draft longer than the whole cap is its own problem and is
    truncated rather than dropped, so an over-long draft still gets a score
    for the part a reader will actually see.
    """
    denial_header = "THE DENIAL:\n"
    letter_header = "\n\nTHE APPEAL LETTER:\n"
    room = DOCUMENT_CHAR_CAP - len(denial_header) - len(letter_header)
    # One redactor for both texts, so a value that appears in each gets the
    # SAME token and one that appears in only the draft gets a NEW one.
    redactor = Redactor(identifiers)
    if (
        len(denial_text or "") > RAW_CHAR_BOUND
        or len(letter_text or "") > RAW_CHAR_BOUND
    ):
        raise LetterScoringError("input over the raw bound; not scored")
    # Redact FIRST, then cut: cutting raw text could split an identifier at
    # the boundary and send its head.
    denial = _cut(redactor.redact(denial_text or "").strip(), room)
    letter = _cut(redactor.redact(letter_text or "").strip(), room)
    denial = _cut(denial, max(0, room - len(letter)))
    return denial_header + denial + letter_header + letter


def parse_answers(payload: typing.Any) -> LetterScore:
    """Turn a System One response into a LetterScore, strictly.

    Strict on purpose: a missing question or an out-of-range score means the
    API changed under us, and a silently wrong number would quietly reorder
    every letter on the site. Better no score.
    """
    try:
        answers = payload["answers"]
        scores: dict[str, float] = {}
        for question in SCORE_QUESTIONS:
            value = float(answers[question]["score"])
            if not 0.0 <= value <= MAX_PER_QUESTION:
                raise LetterScoringError(
                    f"{question} scored {value}, expected 0..{MAX_PER_QUESTION}"
                )
            scores[question] = value
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
    except LetterScoringError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise LetterScoringError(
            f"unexpected response shape: {type(e).__name__}"
        ) from e
    quality = sum(scores.values()) / (MAX_PER_QUESTION * len(SCORE_QUESTIONS))
    return LetterScore(
        quality=quality,
        grounding=scores[GROUNDING_QUESTION],
        scores=scores,
        input_tokens=input_tokens,
        scorer=scorer_for(payload),
    )


async def _post(document: str, timeout_seconds: float) -> typing.Any:
    # Kept as a seam: tests stub this one function to stay off the network.
    return await typesafe.ask(
        document, QUESTIONS, timeout_seconds=timeout_seconds, model=MODEL
    )


async def score_letter(
    denial_text: typing.Optional[str],
    letter_text: typing.Optional[str],
    *,
    identifiers: typing.Iterable[Redaction] = (),
    timeout_seconds: typing.Optional[float] = None,
) -> typing.Optional[LetterScore]:
    """Score one draft, or return None. Never raises, never logs the text.

    ``identifiers`` is everything we hold that could name the patient or the
    professional; see redact(). Pass it. An empty list only means we hold
    nothing, and the generic patterns still run.
    """
    if not enabled():
        _count("skipped")
        return None
    if not (letter_text or "").strip():
        _count("skipped")
        return None
    timeout = timeout_seconds or float(
        getattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 20)
    )
    try:
        payload = await _post(
            build_document(denial_text, letter_text or "", identifiers), timeout
        )
        score = parse_answers(payload)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # The exception text never carries the document: _post raises on
        # status alone and aiohttp's own errors describe the connection.
        _count("failed")
        logger.warning(f"letter scoring unavailable: {type(e).__name__}: {e}")
        return None
    _count("scored")
    return score


def sort_key(
    quality: typing.Optional[float], grounding: typing.Optional[float]
) -> tuple[int, float]:
    """Ordering shared by server and dashboard: grounded before ungrounded,
    then by quality; unscored last. Higher sorts first when reversed."""
    if quality is None:
        return (0, 0.0)
    demoted = grounding is not None and grounding < GROUNDING_DEMOTE_BELOW
    return (1 if demoted else 2, quality)


# How long the stream waits for in-flight scores before the done frame. A
# score that misses this window still lands on the row for the dashboard; the
# reader just sees that draft unranked.
DRAIN_SECONDS = 15.0

# In-flight scoring tasks keyed by (row id, rubric). Two jobs: a strong
# reference so a straggler that outlives its stream (a slow answer after the
# drain window) is not collected mid-request, and a guard so a reconnect on
# this worker does not send the same row to TypeSafe twice while the first
# answer is still in flight. Another worker process can still race; that
# costs a fraction of a cent and last write wins, which is acceptable.
_in_flight: dict[tuple[str, int], "asyncio.Task[typing.Any]"] = {}


def in_flight_task(row_id: str) -> "typing.Optional[asyncio.Task[typing.Any]]":
    """The task already scoring this row on this worker, if any. A second
    stream (a reconnect) attaches to it instead of asking TypeSafe again,
    and still gets the frame when it lands."""
    task = _in_flight.get((str(row_id), RUBRIC_VERSION))
    if task is None or task.done():
        return None
    return task


def in_flight(row_id: str) -> bool:
    return in_flight_task(row_id) is not None


def keep_alive(task: "asyncio.Task[typing.Any]", row_id: str) -> None:
    key = (str(row_id), RUBRIC_VERSION)
    _in_flight[key] = task

    def _forget(done: "asyncio.Future[typing.Any]") -> None:
        # Only if the slot still holds THIS task: a newer task for the same
        # row (after a rubric bump, or a very late straggler) keeps its own.
        if _in_flight.get(key) is done:
            _in_flight.pop(key, None)

    task.add_done_callback(_forget)


def score_frame(proposed_id: str, score: LetterScore) -> dict[str, typing.Any]:
    """The stream frame that carries a score to the client, keyed by row id
    so it can arrive after the letter it belongs to."""
    return {
        "type": "score",
        "id": str(proposed_id),
        "quality_score": round(score.quality, 4),
        "grounding_score": round(score.grounding, 4),
        # The client ranks only drafts scored on ONE scale.
        "scorer": score.scorer,
    }


def with_score_fields(
    frame: dict[str, typing.Any], row: typing.Any
) -> dict[str, typing.Any]:
    """Re-served rows carry their stored score on the letter frame itself,
    while ranking is enabled: the flag is also the kill switch for scores
    already in the database."""
    if not enabled():
        return frame
    quality = getattr(row, "quality_score", None)
    grounding = getattr(row, "grounding_score", None)
    # Numbers only: the frame is JSON, and a row here can be anything that
    # quacks like ProposedAppeal (the citation tests hand in mocks). And only
    # a score from the current rubric: an older scale must not rank against
    # fresh scores (the run rescores such rows instead).
    if (
        isinstance(quality, (int, float))
        and not isinstance(quality, bool)
        and same_rubric(getattr(row, "quality_scorer", None))
    ):
        frame["quality_score"] = round(float(quality), 4)
        frame["scorer"] = str(getattr(row, "quality_scorer", ""))
        frame["grounding_score"] = (
            round(float(grounding), 4)
            if isinstance(grounding, (int, float)) and not isinstance(grounding, bool)
            else None
        )
    return frame


def needs_scoring(row: typing.Any) -> bool:
    """A re-served draft is scored (again) unless it already carries a score
    from the current rubric."""
    text = getattr(row, "appeal_text", None)
    if not isinstance(text, str) or not text.strip():
        return False
    quality = getattr(row, "quality_score", None)
    return quality is None or not same_rubric(getattr(row, "quality_scorer", None))

"""An optional follow-up round of questions on the way to the appeal: once
the person has answered the questions page, do we have enough to write a
strong appeal? If not, ask a few more before generating it.

The first round of questions is written before anyone has answered anything,
and an answer often raises the question that matters ("tried physical
therapy", without saying how it went). One short extra round, asked only when
something is missing, costs the person less than a weaker appeal does.

MLAppealQuestionsHelper.followup_questions_for_denial runs it, once per
procedure and diagnosis:

1. Enough already? A yes/no over the denial, the treatment and condition, the
   health history the person let us use, and their answers. Jev (TypeSafe's
   System One) answers it only with the person's consent to outside models
   (``denial.use_external``), from THE CASE redacted as the letter scorer
   redacts drafts; otherwise, or when Jev is off or fails, one of our own
   models does (``answers_are_enough``).
2. If not, one of our own models writes at most MAX_QUESTIONS follow-ups
   (``get_followup_questions``), or NO_QUESTIONS. Any that repeat a question
   already asked are dropped.

The person's appeal waits on this, so it all runs within TIMEOUT_SECONDS, and
running out of time, a failure, or nothing usable coming back means no
follow-ups. A person who answered nothing is not asked more. Each finished
check is recorded on the denial (``Denial.followup_questions``:
``{"for": questions_fingerprint, "questions": [[question, ""], ...]}``), so it
runs once; a correction to the procedure or diagnosis makes the record stale.

On unless ``FHI_FOLLOWUP_QUESTIONS_ENABLED`` is false/0/no/off (the test
configurations pin it off). The Jev half also needs ``TYPESAFE_API_KEY`` and
``TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED``, which is off by default.
"""

import asyncio
import math
import re
import typing

from django.conf import settings

from fighthealthinsurance.ml import letter_quality, spend, typesafe

# Key of the cross-pod health record (models.ExternalServiceHealth) the
# staff pages read.
SERVICE = "typesafe-followup"

# Jev's "enough" at or above this skips the follow-ups. High on purpose: a
# wrong "enough" costs the person a better letter, a wrong "not enough" only
# costs a model call that can still say none.
ENOUGH_AT = 0.75
# The most follow-ups one round asks. Two keeps it a short extra step.
MAX_QUESTIONS = 2
# The person is waiting on the loading page, which offers to continue by hand
# at 20 seconds (find_next_steps_loading.html): finish before that.
TIMEOUT_SECONDS = 18.0
# Inside that: Jev's request, then our own model's yes/no.
JEV_TIMEOUT_SECONDS = 5.0
LOCAL_GATE_TIMEOUT_SECONDS = 8.0

# qa_context keys that are not the person's answers: the sentence the appeal
# step derives from them, and the review step's own keys
# (denial_context.RESERVED_QA_KEYS, repeated to keep this module free of the
# view layer's imports).
_NOT_ANSWERS = frozenset(
    {"medical_context", "denial date", "date of service", "date_of_service"}
)
# What GenerateAppeal._unticked_checkbox_answers stores for an unticked box.
_UNTICKED = "False"


def enabled() -> bool:
    return bool(getattr(settings, "FHI_FOLLOWUP_QUESTIONS_ENABLED", True))


def jev_enabled() -> bool:
    return typesafe.configured() and bool(
        getattr(settings, "TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED", False)
    )


def answered_pairs(answers: typing.Mapping[str, str]) -> list[tuple[str, str]]:
    """The person's answers, as (question or field, answer), in a stable
    order. Blanks, unticked boxes and keys that are not answers are left
    out, so an empty list means they said nothing."""
    return sorted(
        (str(key), str(value).strip())
        for key, value in answers.items()
        if key not in _NOT_ANSWERS
        and str(value or "").strip()
        and str(value).strip() != _UNTICKED
    )


_NON_WORD = re.compile(r"[^\w]+")


def _normalized(question: str) -> str:
    return _NON_WORD.sub(" ", question.lower()).strip()


def new_candidates(
    candidates: typing.Iterable[tuple[str, str]],
    asked: typing.Iterable[str],
) -> list[tuple[str, str]]:
    """The candidates not already asked (by text, ignoring case and
    punctuation), each once, in order."""
    seen = {_normalized(q) for q in asked if q}
    kept: list[tuple[str, str]] = []
    for question, hint in candidates:
        key = _normalized(question)
        if key and key not in seen:
            seen.add(key)
            kept.append((question, hint))
    return kept


# --- Jev -------------------------------------------------------------------


def build_state(
    denial_text: typing.Optional[str],
    procedure: typing.Optional[str],
    diagnosis: typing.Optional[str],
    history: typing.Optional[str],
    answered: typing.Sequence[tuple[str, str]],
    identifiers: typing.Iterable[letter_quality.Redaction],
) -> str:
    """THE CASE, redacted and under the cap, its parts named in capitals as
    ENOUGH_QUESTION names them. One redactor, so a value in two parts gets
    one token; redact first, then cut, as letter_quality.build_document
    does, so an identifier at the cut never goes out as its head. The
    denial gives way first, cut from its end (a denial is usually
    reason-first)."""
    redactor = letter_quality.Redactor(identifiers)
    answers = "\n".join(f"Q: {q}\nA: {a}" for q, a in answered)
    history = (history or "").strip()
    history_part = f"\n\nTHE HEALTH HISTORY:\n{history}" if history else ""
    rest = redactor.redact(
        f"\n\nTHE TREATMENT: {procedure or '(not given)'}"
        f"\nTHE CONDITION: {diagnosis or '(not given)'}"
        f"{history_part}\n\nTHE ANSWERS:\n{answers or '(none)'}"
    )
    header = "THE DENIAL:\n"
    room = max(0, typesafe.STATE_CHAR_CAP - len(header) - len(rest))
    denial = letter_quality._cut(redactor.redact(denial_text or "").strip(), room)
    return letter_quality._cut(header + denial + rest, typesafe.STATE_CHAR_CAP)


ENOUGH = "enough"

ENOUGH_QUESTION: dict[str, typing.Any] = {
    "type": "noul",
    "instructions": (
        "Do THE ANSWERS and THE HEALTH HISTORY give enough specific facts "
        "about the patient (their condition, the treatments already tried "
        "and how they went, and why THE TREATMENT is needed now) to argue "
        "against the reason THE DENIAL gives?"
    ),
    "criteria": {
        "true": (
            "An appeal could answer the denial's stated reason with facts "
            "about this patient from the answers and history."
        ),
        "false": (
            "Facts an appeal of this denial would need are missing, vague or "
            "only hinted at."
        ),
    },
}


class FollowUpJudgingError(Exception):
    """A response we could not read."""


def parse_enough(payload: typing.Any) -> float:
    """Strict: a missing or odd answer means the API changed under us."""
    try:
        value = payload["answers"][ENOUGH]["noul"]
    except (KeyError, TypeError) as e:
        raise FollowUpJudgingError(
            f"unexpected response shape: {type(e).__name__}"
        ) from e
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FollowUpJudgingError(f"expected a number, got {type(value).__name__}")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise FollowUpJudgingError("answer outside 0..1")
    return float(value)


async def _post(state: str, timeout_seconds: float) -> typing.Any:
    # Kept as a seam: tests stub this one function to stay off the network.
    return await typesafe.ask(
        state,
        {ENOUGH: ENOUGH_QUESTION},
        timeout_seconds=timeout_seconds,
        use=spend.typesafe_use(spend.TRIAGE),
        task="followup_check",
    )


async def judge_enough(
    state: str,
    *,
    on_failure: typing.Optional[typing.Callable[[str], typing.Awaitable[None]]] = None,
) -> typing.Optional[float]:
    """Jev's probability that THE CASE is enough, or None when it is off or
    failed. Never raises, never logs the text. Consent is the caller's to
    check."""
    if not jev_enabled():
        return None
    try:
        return parse_enough(await _post(state, JEV_TIMEOUT_SECONDS))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await letter_quality.report_failure(
            e, what="follow-up check", on_failure=on_failure
        )
        return None


# --- The record on the denial ----------------------------------------------


def checked(record: typing.Any, fingerprint: str) -> bool:
    """Whether the check already ran for these inputs."""
    return isinstance(record, dict) and record.get("for") == fingerprint


def rows(record: typing.Any, fingerprint: str) -> list[list[str]]:
    """The follow-ups asked for these inputs, as generated_questions rows
    ([question, hint]); none for a stale record."""
    if not checked(record, fingerprint):
        return []
    return [
        [row[0], ""]
        for row in record.get("questions") or []
        if isinstance(row, (list, tuple)) and row and isinstance(row[0], str)
    ]


def may_ask(record: typing.Any, fingerprint: str) -> bool:
    """Whether the check could still run for these inputs."""
    return enabled() and not checked(record, fingerprint)


def to_reshow(
    record: typing.Any,
    fingerprint: str,
    posted: typing.Collection[str],
    field_name: typing.Callable[[str], str],
) -> list[tuple[str, str]]:
    """The follow-ups already asked, to show again to a post that carried
    none of their fields (a double submit of the questions page). The
    follow-up page and the questions page on Back both post them, so nobody
    sees the round twice."""
    asked = rows(record, fingerprint)
    if any(field_name(q) in posted for q, _ in asked):
        return []
    return [(q, hint) for q, hint in asked]


def record_for(
    fingerprint: str, questions: typing.Sequence[tuple[str, str]]
) -> dict[str, typing.Any]:
    return {"for": fingerprint, "questions": [[q, ""] for q, _ in questions]}

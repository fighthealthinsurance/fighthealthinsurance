"""Shadow scores for chat replies, from TypeSafe's System One API (Jev).

After a chat reply has been delivered, a background task asks Jev four
questions about that reply, and the same four about the turn's second
answer, each read against the person's latest message, in one request.
Only some turns are scored: every turn that showed a side-by-side (the
person's pick is what the scores are checked against), and a sample of the
others, TYPESAFE_CHAT_SHADOW_SAMPLE_RATE (10% by default). Nothing uses the
answers to choose, order or change a reply: they are stored on the turn's
ChatTurn row, so the staff dashboard can show how Jev rates each model's
replies and how often the answer Jev rates higher is the one the person
picked side by side.

What the numbers are, and are not. The questions below have not been
validated against people's judgement of chat replies; the agreement panel on
the dashboard is that check, and until it says otherwise these are a signal
to read beside the picks, not a quality measure. ``answers_question`` is a
Score from 0 to 2. ``asserts_verdict``, ``asks_again`` and
``promises_outcome`` are yes/no questions, answered as a probability from 0
to 1. ``composite_score`` folds the four into one 0 to 1 number, higher is
better, for the agreement panel.

``promises_outcome`` is our own false-promise rule
(chat/safety_filters.detect_false_promises) put to Jev as a question. The
rule itself stays where it is, so it still holds when Jev is unavailable.

The second answer is the side-by-side alternate when one was shown, and
otherwise the turn's runner-up. The chat (chat_interface.py) never hands
over a turn whose reply a tool rewrote, a document upload or a stored long
paste, or the canned data-deletion reply; chat/shadow_scoring.py runs the
background task.

Data protection. Sent: the person's latest message and the two replies,
redacted exactly as the letter scorer redacts (letter_quality.Redactor): the
identifiers held for the chat and the records linked to it
(chat/redaction.py), and every email address and North American style phone
number in the text, each value with its own stable token. That is a reduction, not de-identification. A request goes out
only when the person allowed outside models for the chat, and the module is
inert until BOTH ``TYPESAFE_API_KEY`` and ``TYPESAFE_CHAT_SHADOW_ENABLED``
are set. Each request is counted against TypeSafe's chat budget
(ml/spend.py), and none starts while that budget is spent. Stored: the scores, the scorer string and an outcome. The text is
never stored and never logged, and a failure is kept as an HTTP status,
"timeout" or an exception class name.
"""

import asyncio
import dataclasses
import math
import random
import re
import typing

from django.conf import settings
from loguru import logger

from fighthealthinsurance.ml import letter_quality, spend, typesafe

# Key of the cross-pod health record (models.ExternalServiceHealth). Its own
# row, so chat traffic never marks letter scoring as healthy or failing.
SERVICE = "typesafe-chat"

# Recorded on every scored row. Bump RUBRIC_VERSION whenever a question or
# the composite changes. The dashboard averages one exact scorer string at a
# time; rows under another rubric or model version are counted, never
# averaged with it. Rubric 2 added PROMISES_OUTCOME; rubric 3 asks about
# both replies in one request.
RUBRIC_VERSION = 3
_RUBRIC_SUFFIX = f"/chat-rubric-{RUBRIC_VERSION}"
SCORER = f"typesafe/{typesafe.DEFAULT_MODEL}{_RUBRIC_SUFFIX}"
_SCORER_RE = re.compile(r"^typesafe/([A-Za-z0-9._-]{1,48})/chat-rubric-(\d{1,4})$")

# Outcomes stored on ChatTurn.shadow_outcome. An empty value means no
# request was made for the turn.
SCORED = "scored"
FAILED = "failed"
TIMEOUT = "timeout"

ANSWERS_QUESTION = "answers_question"
ASSERTS_VERDICT = "asserts_verdict"
ASKS_AGAIN = "asks_again"
PROMISES_OUTCOME = "promises_outcome"

# The state names its two parts in capitals so each question can point at
# them by name (TypeSafe's guidance for jev-1.13, which reads literally).
QUESTIONS: dict[str, dict[str, typing.Any]] = {
    ANSWERS_QUESTION: {
        "type": "score",
        "instructions": (
            "How directly THE REPLY answers what THE PERSON'S MESSAGE asks or "
            "asks for"
        ),
        "criteria": [
            "Does not address what the message asks",
            "Partly addresses it, or answers a different question",
            "Directly answers what the message asks",
        ],
    },
    ASSERTS_VERDICT: {
        "type": "noul",
        "instructions": (
            "Does THE REPLY tell the person, as a settled fact, that their "
            "insurance will or will not cover something, or that they are or "
            "are not eligible for a plan or program?"
        ),
        "criteria": {
            "true": "It states a coverage or eligibility outcome as fact.",
            "false": (
                "It states no such outcome, or explains what could apply and "
                "how to check."
            ),
        },
    },
    ASKS_AGAIN: {
        "type": "noul",
        "instructions": (
            "Does THE REPLY ask the person for a piece of information that "
            "THE PERSON'S MESSAGE already gives?"
        ),
        "criteria": {
            "true": "It asks for something the message already states.",
            "false": (
                "It asks for nothing the message already states, or asks no "
                "questions."
            ),
        },
    },
    PROMISES_OUTCOME: {
        "type": "noul",
        "instructions": (
            "Does THE REPLY promise or guarantee the person a result: that an "
            "appeal, claim or request will be approved, will succeed or will "
            "be won, or that something always works?"
        ),
        "criteria": {
            "true": "It promises or guarantees a result, or says one is certain.",
            "false": (
                "It promises no result: it may say what could help, or how "
                "such cases often go, without guaranteeing an outcome."
            ),
        },
    },
}
MAX_ANSWERS_SCORE = 2.0

MESSAGE_HEADER = "THE PERSON'S MESSAGE:\n"
REPLY_HEADER = "\n\nTHE REPLY:\n"
# With a second answer, the two replies are THE REPLY 1 (the delivered one)
# and THE REPLY 2, and each question is asked once per reply.
PAIR_HEADERS = ("\n\nTHE REPLY 1:\n", "\n\nTHE REPLY 2:\n")
DEFAULT_SAMPLE_RATE = 0.1
STATE_CHAR_CAP = typesafe.STATE_CHAR_CAP
# Longer than this and nothing is sent: the same bound the letter scorer
# uses, because cutting raw text could split an identifier.
RAW_CHAR_BOUND = letter_quality.RAW_CHAR_BOUND
# Added to the request timeout for the hard bound on one turn's requests.
GRACE_SECONDS = 2.0


class ShadowScoringError(Exception):
    """A response we could not turn into scores."""


@dataclasses.dataclass(frozen=True)
class ReplyScores:
    answers: float  # 0..2
    verdict: float  # 0..1, yes = states a verdict
    asks_again: float  # 0..1, yes = asks for what was already given
    promises: float  # 0..1, yes = promises or guarantees a result

    @property
    def composite(self) -> float:
        # parse_answers only builds finite, in-range values, so this is
        # never None.
        value = composite_score(
            self.answers, self.verdict, self.asks_again, self.promises
        )
        return 0.0 if value is None else value


@dataclasses.dataclass(frozen=True)
class ShadowResult:
    outcome: str
    scorer: str = ""
    winner: typing.Optional[ReplyScores] = None
    second: typing.Optional[ReplyScores] = None
    # For the health record: failure_summary of the error, never its text.
    failure: str = ""


def enabled() -> bool:
    return bool(getattr(settings, "TYPESAFE_API_KEY", None)) and bool(
        getattr(settings, "TYPESAFE_CHAT_SHADOW_ENABLED", False)
    )


def budget_allows() -> bool:
    """Whether TypeSafe's chat budget (ml/spend.py) allows a request now."""
    return spend.allows(spend.TYPESAFE, spend.CHAT)


def sample_rate() -> float:
    """The share of turns without a side-by-side that are scored anyway
    (TYPESAFE_CHAT_SHADOW_SAMPLE_RATE, 0 to 1). Anything else means the
    default."""
    value = getattr(settings, "TYPESAFE_CHAT_SHADOW_SAMPLE_RATE", DEFAULT_SAMPLE_RATE)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_SAMPLE_RATE
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return DEFAULT_SAMPLE_RATE
    return number


def _sample_draw() -> float:
    # A seam: tests pin the draw.
    return random.random()


def wanted(side_by_side: bool) -> bool:
    """Whether a delivered turn is scored: always after a side-by-side,
    otherwise on a sample_rate() share of turns."""
    return side_by_side or _sample_draw() < sample_rate()


def scorer_for(payload: typing.Any) -> str:
    """The model TypeSafe says answered, plus the chat rubric version."""
    return f"typesafe/{typesafe.reported_model(payload)}{_RUBRIC_SUFFIX}"


def same_rubric(scorer: typing.Optional[str]) -> bool:
    """Whether a stored scorer string is ours and names the current chat
    rubric, so its scores may be averaged with fresh ones."""
    match = _SCORER_RE.match(str(scorer or ""))
    return match is not None and int(match.group(2)) == RUBRIC_VERSION


def composite_score(
    answers: typing.Optional[float],
    verdict: typing.Optional[float],
    asks_again: typing.Optional[float],
    promises: typing.Optional[float],
) -> typing.Optional[float]:
    """One 0..1 number per reply, higher is better: the four questions
    weighted equally, with a yes to any yes/no question counting against
    the reply. None when any part is missing."""
    values: list[float] = []
    for part in (answers, verdict, asks_again, promises):
        if isinstance(part, bool) or not isinstance(part, (int, float)):
            return None
        if not math.isfinite(part):
            return None
        values.append(float(part))
    a, v, k, p = values
    return (a / MAX_ANSWERS_SCORE + (1.0 - v) + (1.0 - k) + (1.0 - p)) / 4.0


def build_state(message: str, reply: str, redactor: letter_quality.Redactor) -> str:
    """The person's message and one reply, both redacted, under the cap.

    The reply always fits and the message gives way first, cut from the
    end. Redact first, then cut: cutting raw text could split an identifier
    at the boundary and send its head.
    """
    room = STATE_CHAR_CAP - len(MESSAGE_HEADER) - len(REPLY_HEADER)
    redacted_reply = letter_quality._cut(redactor.redact(reply).strip(), room)
    redacted_message = letter_quality._cut(redactor.redact(message).strip(), room)
    redacted_message = letter_quality._cut(
        redacted_message, max(0, room - len(redacted_reply))
    )
    return MESSAGE_HEADER + redacted_message + REPLY_HEADER + redacted_reply


def build_pair_state(
    message: str, reply: str, second: str, redactor: letter_quality.Redactor
) -> str:
    """The person's message and both replies, redacted, under the cap. Each
    reply gets half the room and is cut from its end past it; the message
    gives way first, as in build_state."""
    room = STATE_CHAR_CAP - len(MESSAGE_HEADER) - sum(len(h) for h in PAIR_HEADERS)
    share = max(0, room // 2)
    replies = [
        letter_quality._cut(redactor.redact(text).strip(), share)
        for text in (reply, second)
    ]
    redacted_message = letter_quality._cut(redactor.redact(message).strip(), room)
    redacted_message = letter_quality._cut(
        redacted_message, max(0, room - sum(len(r) for r in replies))
    )
    return (
        MESSAGE_HEADER
        + redacted_message
        + "".join(h + r for h, r in zip(PAIR_HEADERS, replies))
    )


def pair_questions() -> dict[str, dict[str, typing.Any]]:
    """The four questions once per reply, keyed ``<name>_1`` and
    ``<name>_2`` and pointed at THE REPLY 1 or THE REPLY 2."""
    out: dict[str, dict[str, typing.Any]] = {}
    for k in (1, 2):
        for name, question in QUESTIONS.items():
            out[f"{name}_{k}"] = {
                **question,
                "instructions": question["instructions"].replace(
                    "THE REPLY", f"THE REPLY {k}"
                ),
            }
    return out


def _bounded(value: typing.Any, top: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShadowScoringError(f"expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= top:
        raise ShadowScoringError(f"answer outside 0..{top}")
    return number


def parse_answers(payload: typing.Any, suffix: str = "") -> ReplyScores:
    """A System One response as ReplyScores, strictly: a missing answer or a
    value out of range means the API changed under us, so no score.
    ``suffix`` reads one reply of a pair ("_1" or "_2")."""
    try:
        answers = payload["answers"]
        return ReplyScores(
            answers=_bounded(
                answers[ANSWERS_QUESTION + suffix]["score"], MAX_ANSWERS_SCORE
            ),
            verdict=_bounded(answers[ASSERTS_VERDICT + suffix]["noul"], 1.0),
            asks_again=_bounded(answers[ASKS_AGAIN + suffix]["noul"], 1.0),
            promises=_bounded(answers[PROMISES_OUTCOME + suffix]["noul"], 1.0),
        )
    except ShadowScoringError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise ShadowScoringError(
            f"unexpected response shape: {type(e).__name__}"
        ) from e


async def _post(
    state: str,
    timeout_seconds: float,
    questions: typing.Optional[dict[str, dict[str, typing.Any]]] = None,
) -> typing.Any:
    # Kept as a seam: tests stub this one function to stay off the network.
    return await typesafe.ask(
        state,
        QUESTIONS if questions is None else questions,
        timeout_seconds=timeout_seconds,
        use=spend.CHAT,
    )


def _usable(text: typing.Optional[str]) -> bool:
    return bool(text and text.strip()) and len(text or "") <= RAW_CHAR_BOUND


async def score_turn(
    message: typing.Optional[str],
    reply: typing.Optional[str],
    second: typing.Optional[str] = None,
    *,
    identifiers: typing.Iterable[letter_quality.Redaction] = (),
    timeout_seconds: typing.Optional[float] = None,
) -> typing.Optional[ShadowResult]:
    """Score the delivered reply, and the second answer when there is one.

    Returns None when nothing was sent (off, nothing to score, or a text
    over the raw bound), and otherwise a ShadowResult whose outcome is
    SCORED, FAILED or TIMEOUT. Never raises (except cancellation) and never
    logs the text. One request: with a second answer, both replies are in
    it and each question is asked of each, so both come from one model.
    """
    if not enabled():
        return None
    if not _usable(message) or not _usable(reply):
        return None
    if second is not None and not _usable(second):
        second = None
    timeout = timeout_seconds or float(
        getattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 20)
    )
    # One redactor for all three texts, so a value in the message and in a
    # reply gets the SAME token wherever it appears.
    redactor = letter_quality.Redactor(identifiers)
    try:
        if second is None:
            payload = await asyncio.wait_for(
                _post(build_state(message or "", reply or "", redactor), timeout),
                timeout=timeout + GRACE_SECONDS,
            )
            scores = [parse_answers(payload)]
        else:
            payload = await asyncio.wait_for(
                _post(
                    build_pair_state(message or "", reply or "", second, redactor),
                    timeout,
                    pair_questions(),
                ),
                timeout=timeout + GRACE_SECONDS,
            )
            scores = [parse_answers(payload, "_1"), parse_answers(payload, "_2")]
        scorer = scorer_for(payload)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        timed_out = isinstance(e, (TimeoutError, asyncio.TimeoutError))
        (logger.debug if typesafe.announced(e) else logger.warning)(
            f"chat shadow scoring unavailable: {type(e).__name__}"
        )
        return ShadowResult(
            outcome=TIMEOUT if timed_out else FAILED,
            failure=letter_quality.failure_summary(e),
        )
    return ShadowResult(
        outcome=SCORED,
        scorer=scorer,
        winner=scores[0],
        second=scores[1] if len(scores) > 1 else None,
    )

"""A live check on one of our own chat replies, from TypeSafe's System One
API (Jev), before the paid outside models are asked.

This is the cascade TypeSafe documents (docs.typesafe.ai/cookbooks/
sde_cascade): the cheap model answers first, Jev checks the answer, and the
expensive models are asked only when the check does not pass. In chat,
"cheap" is our own models and "expensive" is the outside ones.
chat/reply_gate.py runs it inside the chat fan-out; this module holds our
own checks, the questions, the decision rule and the request.

Our own checks come first. Before anything is sent, the reply must pass
the rule the retry uses (chat/retry_handler.should_retry_response): not
empty, at least MIN_RESPONSE_LENGTH characters, and no promised outcome
(chat/safety_filters.detect_false_promises). A reply that fails them fails
the check with the scorer LOCAL_SCORER and is never sent to TypeSafe, so
these requirements hold whether or not Jev can be reached.

The questions. Four yes/no ("noul") questions about the reply, read
against the person's latest message, written for Jev's literal reading
(docs.typesafe.ai/model-jaggedness/jev-1.13): the state names its two parts
in capitals and each question points at them by name.

* ``answers_question``: does the reply respond to what the message asks or
  says? Yes is good.
* ``states_verdict``: does the reply state a coverage or eligibility outcome
  as a settled fact? Yes is a problem: the reply cannot know that.
* ``asks_again``: does the reply ask for something the message already
  gives? Yes is a problem.
* ``promises_outcome``: does the reply promise or guarantee a result, such
  as an approval or a win? Yes is a problem: the same rule as our own
  false-promise check, asked of Jev too.

The decision rule. A reply passes when ``answers_question`` is at least
FHI_CHAT_JEV_GATE_MIN_ANSWERS (default 0.7) and each problem answer is
below FHI_CHAT_JEV_GATE_MAX_PROBLEM (default 0.3). Anything else is a fail.
A failed reply is ranked just below the outside models' answers while
FHI_CHAT_JEV_GATE_DEMOTE_FAILED is on (the default).
These questions and thresholds have not been validated against people's
judgement of chat replies; the staff dashboard shows how often a failed
check was followed by an outside model's answer being delivered, which is
the first check on them.

Data protection. Sent: the person's latest message and our reply, redacted
exactly as the letter scorer redacts (letter_quality.Redactor): the
identifiers held for the chat's accounts and the appeals and prior
authorization requests linked to it (chat/redaction.py), and every email
address and North American style phone number in the text, each value with
its own stable token. That is a reduction, not de-identification. A request goes out
only when the person allowed outside models for the chat, and the module is
inert until BOTH ``TYPESAFE_API_KEY`` and ``FHI_CHAT_JEV_GATE_ENABLED`` are
set. Kept: the four numbers, the scorer string and an outcome. The text is
never stored and never logged, and a failure is kept as an HTTP status,
"timeout" or an exception class name.
"""

import asyncio
import dataclasses
import math
import re
import typing

from django.conf import settings
from loguru import logger

from fighthealthinsurance.chat.retry_handler import should_retry_response
from fighthealthinsurance.ml import letter_quality, typesafe

# Key of the cross-pod health record (models.ExternalServiceHealth). Its own
# row, so chat checks never mark letter scoring as healthy or failing.
SERVICE = "typesafe-chat-gate"

# Recorded with every answered check. Bump RUBRIC_VERSION whenever a question
# changes, so rows under another rubric can be told apart.
RUBRIC_VERSION = 2
_RUBRIC_SUFFIX = f"/chat-gate-rubric-{RUBRIC_VERSION}"
SCORER = f"typesafe/{typesafe.DEFAULT_MODEL}{_RUBRIC_SUFFIX}"
_SCORER_RE = re.compile(r"^typesafe/([A-Za-z0-9._-]{1,48})/chat-gate-rubric-(\d{1,4})$")

# Recorded instead when our own checks failed the reply before Jev was asked
# (fails_our_checks). Bump the number whenever those checks change.
LOCAL_SCORER = "fhi/local-checks-1"
_LOCAL_SCORER_RE = re.compile(r"^fhi/local-checks-\d{1,4}$")

# Outcomes stored on ChatTurn.gate_outcome. pass: Jev answered and the reply
# met the thresholds. fail: Jev answered and it did not, or our own checks
# failed the reply before anything was sent. error: the request
# failed, was refused or came back in a shape we could not read. timeout: no
# answer in time. skipped: the check was on for the turn but nothing was
# judged (no usable reply of ours in time, or one that could not be judged).
PASS = "pass"
FAIL = "fail"
ERROR = "error"
TIMEOUT = "timeout"
SKIPPED = "skipped"
OUTCOMES = (PASS, FAIL, ERROR, TIMEOUT, SKIPPED)

ANSWERS_QUESTION = "answers_question"
STATES_VERDICT = "states_verdict"
ASKS_AGAIN = "asks_again"
PROMISES_OUTCOME = "promises_outcome"

QUESTIONS: dict[str, dict[str, typing.Any]] = {
    ANSWERS_QUESTION: {
        "type": "noul",
        "instructions": (
            "Does THE REPLY respond to what THE PERSON'S MESSAGE asks, says "
            "or requests?"
        ),
        "criteria": {
            "true": (
                "It addresses what the message asks or says: it answers the "
                "question, does what was asked, or takes the next step with "
                "what the message gives."
            ),
            "false": (
                "It ignores what the message asks or says, answers a "
                "different question, or asks again for what the message "
                "already answers."
            ),
        },
    },
    STATES_VERDICT: {
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

MESSAGE_HEADER = "THE PERSON'S MESSAGE:\n"
REPLY_HEADER = "\n\nTHE REPLY:\n"
STATE_CHAR_CAP = typesafe.STATE_CHAR_CAP
# Longer than this and nothing is sent: the same bound the letter scorer
# uses, because cutting raw text could split an identifier.
RAW_CHAR_BOUND = letter_quality.RAW_CHAR_BOUND

# Defaults and bounds of the owner's knobs (settings.py reads the same
# defaults from the environment). A setting outside its bounds, or not a
# number, falls back to the default here too.
DEFAULT_MAX_WAIT_SECONDS = 8.0
MAX_WAIT_BOUNDS = (0.5, 30.0)
DEFAULT_TIMEOUT_SECONDS = 1.5
TIMEOUT_BOUNDS = (0.2, 10.0)
DEFAULT_MIN_ANSWERS = 0.7
DEFAULT_MAX_PROBLEM = 0.3
THRESHOLD_BOUNDS = (0.0, 1.0)


class ChatGateError(Exception):
    """A response we could not turn into answers."""


@dataclasses.dataclass(frozen=True)
class GateScores:
    answers: float  # 0..1, yes = responds to the message
    verdict: float  # 0..1, yes = states a coverage or eligibility verdict
    asks_again: float  # 0..1, yes = asks for what the message already gives
    promises: float  # 0..1, yes = promises or guarantees a result


@dataclasses.dataclass(frozen=True)
class GateResult:
    outcome: str
    scores: typing.Optional[GateScores] = None
    scorer: str = ""
    # For the health record: failure_summary of the error, never its text.
    failure: str = ""

    @property
    def answered(self) -> bool:
        """Jev answered and the answers were read (pass or fail)."""
        return self.outcome in (PASS, FAIL)


def enabled() -> bool:
    """The switch and the key. The chat adds the person's consent, and
    whether the turn has outside models to hold back."""
    return bool(getattr(settings, "TYPESAFE_API_KEY", None)) and bool(
        getattr(settings, "FHI_CHAT_JEV_GATE_ENABLED", False)
    )


def _setting(name: str, default: float, bounds: typing.Tuple[float, float]) -> float:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    low, high = bounds
    if not math.isfinite(number) or not low <= number <= high:
        return default
    return number


def max_wait_seconds() -> float:
    """How long the fan-out holds the outside models back for the check."""
    return _setting(
        "FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS, MAX_WAIT_BOUNDS
    )


def timeout_seconds() -> float:
    """How long one check may take, identifier lookup included."""
    return _setting(
        "FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, TIMEOUT_BOUNDS
    )


def min_answers() -> float:
    return _setting(
        "FHI_CHAT_JEV_GATE_MIN_ANSWERS", DEFAULT_MIN_ANSWERS, THRESHOLD_BOUNDS
    )


def max_problem() -> float:
    return _setting(
        "FHI_CHAT_JEV_GATE_MAX_PROBLEM", DEFAULT_MAX_PROBLEM, THRESHOLD_BOUNDS
    )


def demote_failed() -> bool:
    """Whether a reply of ours that fails the check is ranked just below the
    outside models' answers (FHI_CHAT_JEV_GATE_DEMOTE_FAILED, on by
    default). Anything but a boolean setting means the default."""
    value = getattr(settings, "FHI_CHAT_JEV_GATE_DEMOTE_FAILED", True)
    return value if isinstance(value, bool) else True


def passes(scores: GateScores) -> bool:
    """The decision rule: the reply responds to the message (at least
    min_answers) and no problem is likely (each below max_problem)."""
    limit = max_problem()
    return (
        scores.answers >= min_answers()
        and scores.verdict < limit
        and scores.asks_again < limit
        and scores.promises < limit
    )


def fails_our_checks(reply: typing.Optional[str]) -> bool:
    """Whether our own requirements reject the reply, by the rule the retry
    uses (chat/retry_handler.should_retry_response): empty, shorter than
    MIN_RESPONSE_LENGTH, or a false promise. No request is involved, so it
    works whether or not Jev can be reached."""
    return should_retry_response(reply)


def scorer_for(payload: typing.Any) -> str:
    """The model TypeSafe says answered, plus the gate rubric version."""
    return f"typesafe/{typesafe.reported_model(payload)}{_RUBRIC_SUFFIX}"


def same_rubric(scorer: typing.Optional[str]) -> bool:
    """Whether a stored scorer string is ours and names the current rubric."""
    match = _SCORER_RE.match(str(scorer or ""))
    return match is not None and int(match.group(2)) == RUBRIC_VERSION


def from_our_checks(scorer: typing.Optional[str]) -> bool:
    """Whether a stored scorer string says our own checks failed the reply
    before Jev was asked, under any version of those checks."""
    return _LOCAL_SCORER_RE.match(str(scorer or "")) is not None


def judgeable(text: typing.Optional[str]) -> bool:
    """Something to send: not empty, and short enough to redact whole."""
    return bool(text and text.strip()) and len(text or "") <= RAW_CHAR_BOUND


def build_state(message: str, reply: str, redactor: letter_quality.Redactor) -> str:
    """The person's message and our reply, both redacted, under the cap.

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


def _probability(value: typing.Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChatGateError(f"expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ChatGateError("answer outside 0..1")
    return number


def parse_answers(payload: typing.Any) -> GateScores:
    """A System One response as GateScores, strictly: a missing answer or a
    value out of range means the API changed under us, so no decision."""
    try:
        answers = payload["answers"]
        return GateScores(
            answers=_probability(answers[ANSWERS_QUESTION]["noul"]),
            verdict=_probability(answers[STATES_VERDICT]["noul"]),
            asks_again=_probability(answers[ASKS_AGAIN]["noul"]),
            promises=_probability(answers[PROMISES_OUTCOME]["noul"]),
        )
    except ChatGateError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise ChatGateError(f"unexpected response shape: {type(e).__name__}") from e


async def _post(state: str, timeout: float) -> typing.Any:
    # Kept as a seam: tests stub this one function to stay off the network.
    return await typesafe.ask(state, QUESTIONS, timeout_seconds=timeout)


async def check_reply(
    message: typing.Optional[str],
    reply: typing.Optional[str],
    *,
    identifiers: typing.Iterable[letter_quality.Redaction] = (),
    timeout: float,
) -> GateResult:
    """Ask Jev about one reply of ours and apply the decision rule.

    Returns SKIPPED without sending anything when the gate is off or either
    text is empty or over the raw bound. Otherwise PASS or FAIL when Jev
    answered, TIMEOUT when it did not answer within ``timeout`` seconds, and
    ERROR for anything else (an HTTP error, a refused request, a response we
    could not read). Never raises (except cancellation) and never logs the
    text.
    """
    if not enabled() or not judgeable(message) or not judgeable(reply):
        return GateResult(outcome=SKIPPED)
    # One redactor for both texts, so a value in the message and in the
    # reply gets the SAME token in both parts.
    redactor = letter_quality.Redactor(identifiers)
    state = build_state(message or "", reply or "", redactor)
    try:
        payload = await asyncio.wait_for(_post(state, timeout), timeout=timeout)
        scores = parse_answers(payload)
        scorer = scorer_for(payload)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        timed_out = isinstance(e, (TimeoutError, asyncio.TimeoutError))
        logger.warning(f"Chat reply check unavailable: {type(e).__name__}")
        return GateResult(
            outcome=TIMEOUT if timed_out else ERROR,
            failure=letter_quality.failure_summary(e),
        )
    return GateResult(
        outcome=PASS if passes(scores) else FAIL, scores=scores, scorer=scorer
    )

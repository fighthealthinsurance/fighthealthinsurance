"""Transport for TypeSafe's System One API, shared by every feature that asks it
typed questions about a piece of text (draft scoring in letter_quality.py, and
denial triage). One place for the URL, the auth header, the model, the timeout
and the status check, so a feature module only decides WHAT to ask.

The request follows the documented API (docs.typesafe.ai/api): a JSON body of
``state`` (the text to evaluate), ``model`` and ``questions``, with the key as
a Bearer token.

Never logs the state. A response body is never surfaced either: on a non-200
the error carries the status alone, because an error body could quote the
state back. (The body of a 400, 401, 403 or 429 is read, only to tell a
credit or quota refusal, and goes no further.) And the request, which carries
the API key and the state, only ever goes over https: a URL with any other
scheme is refused before a session exists.

A refused key, an unknown or retired model, or an endpoint that cannot be
reached fails every request the same way, so it starts a short cooldown
(FHI_TYPESAFE_COOLDOWN_SECONDS) shared by every use in this process, rather
than each denial, draft and chat turn paying for the same failure.
"""

import math
import re
import threading
import time
import typing
from urllib.parse import urlsplit

import aiohttp
from django.conf import settings
from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable
from fighthealthinsurance.ml import spend

# The pinned Jev release, used when TYPESAFE_MODEL is unset or empty. A pinned
# id rather than the "jev-latest" alias, because TypeSafe can repoint an alias
# to a new release and that moves every score we record. Features record the
# model the response names, so a change here shows up in their data as a new
# series rather than a silently different number.
DEFAULT_MODEL = "jev-1.13.0"

# A model id or alias as TypeSafe spells them ("jev-1.13.0", "jev-latest").
# The same shape the features' provenance strings allow, so a model name can
# never break or forge one.
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,48}")

# Measured in the evaluation run, and well inside the documented budget of
# 32k tokens for the state plus the longest question.
STATE_CHAR_CAP = 24_000


class TypeSafeError(Exception):
    """The API did not return a usable answer set.

    ``status`` is the HTTP status when the API answered at all, so a caller
    can tell a rejected key (401), exhausted credits (402), a request that
    failed validation (422), a rate limit (429) or an overload (529) from an
    outage (other 5xx) without parsing the message.
    """

    def __init__(self, message: str, status: typing.Optional[int] = None):
        super().__init__(message)
        self.status = status


class TypeSafeBudgetSpent(TypeSafeError):
    """Not sent: this use's TypeSafe budget is spent (ml/spend.py)."""


class TypeSafeCoolingDown(TypeSafeError):
    """Not sent: TypeSafe refused the key, did not know the model or could
    not be reached moments ago (FHI_TYPESAFE_COOLDOWN_SECONDS).

    ``status`` is the HTTP status that started the cooldown, so a status
    page keeps showing the cause; None when TypeSafe could not be reached.
    """


# Answers that mean every request will fail the same way for a while: a
# rejected key (401, 403) or a model or endpoint TypeSafe does not know or
# has retired (404, 410). Not a 422: one bad request can earn that.
COOLDOWN_STATUSES = frozenset({401, 403, 404, 410})

# The statuses spend.quota_refusal reads a body for (a 402 needs none).
_QUOTA_BODY_STATUSES = frozenset({400, 401, 403, 429})

DEFAULT_COOLDOWN_SECONDS = 900.0
# Not reaching TypeSafe at all is more often a passing blip (DNS, a proxy
# restart) than a refusal is, so it holds requests back for less: drafts made
# meanwhile go unscored.
CONNECT_COOLDOWN_SECONDS = 120.0

# (monotonic deadline, the status that started it): one tuple, replaced
# whole, so a reader never pairs one cooldown's deadline with another's
# status. The lock only keeps the "started" WARNING to one per cooldown.
_cooldown: tuple[float, typing.Optional[int]] = (float("-inf"), None)
_cooldown_lock = threading.Lock()


def cooldown_seconds() -> float:
    """FHI_TYPESAFE_COOLDOWN_SECONDS, from the Django settings or else the
    environment; 900 when unset or not a non-negative number."""
    raw: typing.Any = getattr(settings, "FHI_TYPESAFE_COOLDOWN_SECONDS", None)
    if raw is None:
        raw = get_env_variable("FHI_TYPESAFE_COOLDOWN_SECONDS")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS
    if not math.isfinite(seconds) or seconds < 0:
        return DEFAULT_COOLDOWN_SECONDS
    return seconds


def _start_cooldown(
    cause: str, status: typing.Optional[int], cap: float = math.inf
) -> None:
    global _cooldown
    seconds = min(cooldown_seconds(), cap)
    with _cooldown_lock:
        now = time.monotonic()
        already = now < _cooldown[0]
        _cooldown = (now + seconds, status)
    if already:
        # A request sent before the cooldown began, failing the same way.
        logger.debug(f"TypeSafe {cause}; cooldown restarted")
    else:
        logger.warning(f"TypeSafe {cause}; not asking it again for {seconds:.0f}s")


def _end_cooldown() -> None:
    """TypeSafe answered (a request sent before the cooldown began): ask it
    again at once."""
    global _cooldown
    with _cooldown_lock:
        _cooldown = (float("-inf"), None)


def _refuse_while_cooling() -> None:
    until, status = _cooldown
    left = until - time.monotonic()
    if left > 0:
        logger.debug(f"TypeSafe not asked: cooling down for {left:.0f}s more")
        raise TypeSafeCoolingDown("cooling down", status=status)


def reset_cooldown_for_tests() -> None:
    global _cooldown
    with _cooldown_lock:
        _cooldown = (float("-inf"), None)


async def _note_refusal(response: typing.Any) -> None:
    """Pause or cool TypeSafe for a non-200 that will repeat. Never raises."""
    status = response.status
    body = ""
    if status in _QUOTA_BODY_STATUSES:
        try:
            # Read only to tell a credit or quota refusal: it may quote the
            # state back, so it is never logged or kept.
            body = await response.text(errors="replace")
        except Exception:
            body = ""
    if spend.quota_refusal(status, body):
        spend.pause(
            spend.TYPESAFE,
            reason=f"TypeSafe refused for credit or quota, HTTP {status}",
        )
    if status in COOLDOWN_STATUSES:
        _start_cooldown(f"answered HTTP {status}", status)


def configured() -> bool:
    return bool(getattr(settings, "TYPESAFE_API_KEY", None))


def model_name() -> str:
    """The model every request names: TYPESAFE_MODEL, or the pinned release
    when the setting is unset or empty."""
    return str(getattr(settings, "TYPESAFE_MODEL", "") or "").strip() or DEFAULT_MODEL


def reported_model(payload: typing.Any) -> str:
    """The model a response says answered, for the provenance strings the
    features record. TypeSafe reports the versioned id (jev-1.13.0 even when
    the request named the jev-latest alias), so a repointed alias shows up as
    a new name. A response that names none, or names something that is not a
    plain model name (the API types it as a string), is recorded under the
    name the request sent."""
    answered = payload.get("model") if isinstance(payload, dict) else None
    name = answered.strip() if isinstance(answered, str) else ""
    return name if _MODEL_NAME.fullmatch(name) else model_name()


async def ask(
    state: str,
    questions: dict[str, dict[str, typing.Any]],
    *,
    timeout_seconds: float,
    use: str = spend.OTHER,
) -> typing.Any:
    """POST one state and a set of typed questions; return the raw JSON.

    ``use`` names the feature spending (spend.LETTERS, spend.TRIAGE,
    spend.CHAT): the request is refused before sending when that use's
    TypeSafe budget is spent (ml/spend.py), and the input tokens the answer
    reports are counted against it. A credit or quota refusal (an HTTP 402,
    or spend.quota_refusal) pauses TypeSafe for every use until the next UTC
    day. An HTTP 401, 403, 404 or 410, or an endpoint that cannot be reached
    (DNS, refused, TLS), starts the cooldown: until it ends every use is
    refused with TypeSafeCoolingDown before anything is sent.

    Raises TypeSafeError on a non-200, a spent budget or a cooldown, and
    lets aiohttp/asyncio errors propagate: callers decide what a failure
    means for their feature.
    """
    if not spend.allows(spend.TYPESAFE, use):
        raise TypeSafeBudgetSpent("budget spent")
    url = str(getattr(settings, "TYPESAFE_API_URL", "") or "")
    if urlsplit(url).scheme.lower() != "https":
        # The bearer token and the state must never travel in the clear
        # (review). Refused here, before anything is built or sent.
        raise TypeSafeError("TYPESAFE_API_URL must use https")
    model = model_name()
    if not _MODEL_NAME.fullmatch(model):
        # Refused rather than sent: when a response names no model, this name
        # goes into the recorded provenance, so it must fit that format.
        raise TypeSafeError("TYPESAFE_MODEL is not a model name")
    _refuse_while_cooling()
    body = {
        "state": state[:STATE_CHAR_CAP],
        "model": model,
        "questions": questions,
    }
    headers = {
        "Authorization": f"Bearer {settings.TYPESAFE_API_KEY}",
        "Content-Type": "application/json",
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            # No redirects: a 307 or 308 toward http would make aiohttp resend
            # the state in the clear (review). A 3xx is just a non-200 here.
            async with session.post(
                url, json=body, headers=headers, allow_redirects=False
            ) as response:
                if response.status != 200:
                    await _note_refusal(response)
                    raise TypeSafeError(
                        f"HTTP {response.status}", status=response.status
                    )
                _end_cooldown()
                payload = await response.json()
    except aiohttp.ClientConnectorError as e:
        # The connect phase (DNS, refused, TLS) failed: nothing reached
        # TypeSafe, and the next request would likely fail the same way.
        _start_cooldown(
            f"could not be reached ({type(e).__name__})",
            None,
            cap=CONNECT_COOLDOWN_SECONDS,
        )
        raise
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict):
        spend.record(
            spend.TYPESAFE, use, spend.typesafe_cost_micro(usage.get("input_tokens"))
        )
    return payload

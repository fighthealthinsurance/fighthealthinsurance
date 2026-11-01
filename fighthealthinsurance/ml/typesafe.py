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

import datetime
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
from fighthealthinsurance.ml import llm_usage, spend

# The pinned Jev release, used when TYPESAFE_MODEL is unset or empty. A pinned
# id rather than the "jev-latest" alias, because TypeSafe can repoint an alias
# to a new release and that moves every score we record. Features record the
# model the response names, so a change here shows up in their data as a new
# series rather than a silently different number.
DEFAULT_MODEL = "jev-1.13.0"
OPENCODE_MODEL = "jev-1.13-free"
OPENCODE_API_URL = "https://opencode.ai/inference/systemone/v1/systemone"

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
    not be reached moments ago (FHI_TYPESAFE_COOLDOWN_SECONDS; a cooldown
    a failed connection started is capped at CONNECT_COOLDOWN_SECONDS).

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
# The longest the connect (DNS, then TCP and TLS over every address the host
# has) may take, inside the request's own timeout. A host that drops our
# packets then fails as a connect timeout, which counts toward the cooldown,
# rather than as the request's timeout, which does not (a slow answer from a
# reachable host is no reason to stop asking).
CONNECT_TIMEOUT_SECONDS = 10.0
# A refused connection is definitive and cools at once; a connect timeout is
# not (one lost packet can cause it inside the chat gate's ~1s connect
# bound, and the cooldown stops letter scoring and triage too). So it starts
# the cooldown only as the CONNECT_TIMEOUT_STREAK-th in a row, all within
# CONNECT_TIMEOUT_WINDOW_SECONDS of the first. Any HTTP answer ends a streak.
CONNECT_TIMEOUT_STREAK = 2
CONNECT_TIMEOUT_WINDOW_SECONDS = 60.0
# A connect timeout counts toward that streak only when the connect had at
# least this long. A shorter bound comes from a caller's leftover budget (the
# chat gate's, after its identifier lookup ate most of it, or a chat timeout
# configured low), and a healthy host's TCP and TLS handshake can miss it:
# that says nothing about whether TypeSafe can be reached, and counting it
# would let one squeezed chat turn stop letter scoring and triage too.
MIN_COUNTED_CONNECT_SECONDS = 1.0

# (monotonic deadline, the status that started it): one tuple, replaced
# whole, so a reader never pairs one cooldown's deadline with another's
# status. The lock keeps the "started" WARNING to one per cooldown, and the
# spent-budget WARNING below to one per use and day.
_cooldown: tuple[float, typing.Optional[int]] = (float("-inf"), None)
_cooldown_lock = threading.Lock()

# Each use's UTC day on which its spent budget was last logged as a WARNING;
# later refusals that day go to debug. Guarded by _cooldown_lock.
_budget_spent_logged: dict[str, datetime.date] = {}

# (connect timeouts in a row, monotonic time of the first): the streak a
# connect timeout needs before it cools. Guarded by _cooldown_lock.
_connect_timeouts: tuple[int, float] = (0, float("-inf"))


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


def cooling_down() -> bool:
    """Whether a cooldown is in force: ask() would refuse every request with
    TypeSafeCoolingDown before anything is sent. For callers that would
    otherwise do work of their own (a database lookup, holding a chat turn
    back) before asking. False on the free OpenCode route: the cooldown
    refuses only the premium attempt, and that route still answers."""
    if opencode_route():
        return False
    return time.monotonic() < _cooldown[0]


def _log_budget_spent(use: str) -> None:
    """A spent or paused budget refuses every request for the rest of the
    day or month, so it is a WARNING once per use and UTC day, then debug.
    A credit pause was announced by spend.pause already; a monthly cap
    running out is announced nowhere else."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    with _cooldown_lock:
        first = _budget_spent_logged.get(use) != today
        _budget_spent_logged[use] = today
    if first:
        logger.warning(
            f"TypeSafe not asked for {use}: its budget is spent or paused, or "
            "the spend ledger is unread (ml/spend.py); further refusals today "
            "are logged at debug"
        )
    else:
        logger.debug(f"TypeSafe not asked for {use}: budget spent or paused")


def refused_before_sending(e: BaseException) -> bool:
    """Whether ask() raised ``e`` before anything was sent: a spent or
    paused budget, a cooldown, or a setting it refuses (settings_problem).
    A cooldown carries the status that started it, so it is told apart by
    class, not by a missing status."""
    return isinstance(e, (TypeSafeBudgetSpent, TypeSafeCoolingDown)) or (
        isinstance(e, TypeSafeError) and e.status is None
    )


def announced(e: BaseException) -> bool:
    """Whether ``e`` is a refusal whose cause was logged once when it began:
    a cooldown (_start_cooldown) or a spent or paused budget
    (_log_budget_spent, spend.pause). Callers log these at debug and
    anything else at WARNING."""
    return isinstance(e, (TypeSafeCoolingDown, TypeSafeBudgetSpent))


def _connect_timed_out() -> bool:
    """Count one connect timeout; True when it completes a streak (which
    then starts over)."""
    global _connect_timeouts
    with _cooldown_lock:
        now = time.monotonic()
        count, first = _connect_timeouts
        if now - first > CONNECT_TIMEOUT_WINDOW_SECONDS:
            count, first = 0, now
        count += 1
        if count >= CONNECT_TIMEOUT_STREAK:
            _connect_timeouts = (0, float("-inf"))
            return True
        _connect_timeouts = (count, first)
        return False


def _clear_connect_timeouts() -> None:
    """TypeSafe answered (any HTTP status): the connect works, so a connect
    timeout streak starts over."""
    global _connect_timeouts
    with _cooldown_lock:
        _connect_timeouts = (0, float("-inf"))


def reset_cooldown_for_tests() -> None:
    global _cooldown, _connect_timeouts
    with _cooldown_lock:
        _cooldown = (float("-inf"), None)
        _budget_spent_logged.clear()
        _connect_timeouts = (0, float("-inf"))


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


# Why nothing is sent anywhere without a key (every feature checks
# configured() before asking). With a custom TYPESAFE_MODEL only
# TYPESAFE_API_KEY counts, so the message names what each setup needs.
NO_KEY = (
    "no TypeSafe key is set: TYPESAFE_API_KEY for a custom TYPESAFE_MODEL, "
    "or OPENCODE_TOKEN or TYPESAFE_API_KEY for the default model"
)


def configured() -> bool:
    if model_name() != DEFAULT_MODEL:
        return bool(getattr(settings, "TYPESAFE_API_KEY", None))
    return bool(
        getattr(settings, "OPENCODE_TOKEN", None)
        or getattr(settings, "TYPESAFE_API_KEY", None)
    )


def model_name() -> str:
    """The model every request names: TYPESAFE_MODEL, or the pinned release
    when the setting is unset or empty."""
    return str(getattr(settings, "TYPESAFE_MODEL", "") or "").strip() or DEFAULT_MODEL


def opencode_route() -> bool:
    """Whether ask() tries OpenCode first: the token is set and the model is
    the pinned base release, the only one OpenCode serves. That route is
    free, spends no TypeSafe budget, and a TypeSafe cooldown refuses only the
    premium attempt, so none of those states holds it back."""
    return bool(getattr(settings, "OPENCODE_TOKEN", None)) and (
        model_name() == DEFAULT_MODEL
    )


def settings_problem() -> typing.Optional[str]:
    """Why ask() refuses to send whatever it is asked, from its settings
    alone, or None. Every endpoint ask() may post to is checked: the premium
    fallback carries the TypeSafe bearer token and the state too."""
    urls: list[str] = []
    if opencode_route():
        urls.append(OPENCODE_API_URL)
        if getattr(settings, "TYPESAFE_API_KEY", None):
            # A failing OpenCode attempt falls through to this URL.
            urls.append(str(getattr(settings, "TYPESAFE_API_URL", "") or ""))
    else:
        urls.append(str(getattr(settings, "TYPESAFE_API_URL", "") or ""))
    for url in urls:
        if urlsplit(url).scheme.lower() != "https":
            # The bearer token and the state must never travel in the clear
            # (review).
            return "TYPESAFE_API_URL must use https"
    if not _MODEL_NAME.fullmatch(model_name()):
        # When a response names no model, this name goes into the recorded
        # provenance, so it must fit that format.
        return "TYPESAFE_MODEL is not a model name"
    return None


def skip_reason(use: str = spend.OTHER) -> typing.Optional[str]:
    """Why a request for ``use`` would not be sent from this process right
    now, or None: no API key, or what ask() refuses before sending, in the
    order it checks. For the staff pages, which must say when Jev is being
    skipped and why: reads settings and this process's state, never waits
    and never sends."""
    if not configured():
        return NO_KEY
    free_route = opencode_route()
    if not free_route and (
        spend.paused(spend.TYPESAFE, "*") or spend.paused(spend.TYPESAFE, use)
    ):
        return "paused until the next UTC day: TypeSafe refused for credit or quota"
    if not free_route and not spend.loaded():
        return "this process's spend ledger has not loaded (ml/spend.py)"
    if not free_route and not spend.allows(spend.TYPESAFE, use):
        for_use = "" if use == spend.OTHER else f" for {use}"
        return (
            f"TypeSafe's budget is spent{for_use} "
            "(FHI_SPEND_TYPESAFE_* in ml/spend.py)"
        )
    problem = settings_problem()
    if problem:
        return problem
    until, status = _cooldown
    left = until - time.monotonic()
    if left > 0 and not free_route:
        # A cooldown refuses only the premium attempt; the free OpenCode
        # route still answers.
        cause = f"HTTP {status}" if status is not None else "a failed connection"
        return f"cooling down for {left:.0f}s more after {cause}"
    return None


def reported_model(payload: typing.Any, default: typing.Optional[str] = None) -> str:
    """The model a response says answered, for the provenance strings the
    features record. TypeSafe reports the versioned id (jev-1.13.0 even when
    the request named the jev-latest alias), so a repointed alias shows up as
    a new name. A response that names none, or names something that is not a
    plain model name (the API types it as a string), is recorded under the
    name the request sent: ``default`` when given (the OpenCode route names
    jev-1.13-free, not the pinned premium release), else model_name()."""
    answered = payload.get("model") if isinstance(payload, dict) else None
    name = answered.strip() if isinstance(answered, str) else ""
    return name if _MODEL_NAME.fullmatch(name) else (default or model_name())


async def ask(
    state: str,
    questions: dict[str, dict[str, typing.Any]],
    *,
    timeout_seconds: float,
    use: str = spend.OTHER,
    task: str = "other",
) -> typing.Any:
    """POST one state and a set of typed questions; return the raw JSON.

    ``use`` names the feature spending (spend.LETTERS, spend.TRIAGE,
    spend.CHAT): the request is refused before sending when that use's
    TypeSafe budget is spent (ml/spend.py), and the input tokens the answer
    reports are counted against it. A credit or quota refusal (an HTTP 402,
    or spend.quota_refusal) pauses TypeSafe for every use until the next UTC
    day. An HTTP 401, 403, 404 or 410, an endpoint that cannot be reached
    (DNS, refused, TLS), or a connect that times out twice in a row
    (CONNECT_TIMEOUT_STREAK; a connect bound under
    MIN_COUNTED_CONNECT_SECONDS never counts) starts the cooldown: until it
    ends every use is refused with TypeSafeCoolingDown before anything is
    sent. The free OpenCode route has no cooldown or backoff of its own:
    while OpenCode keeps failing, every eligible request tries it again
    before falling through to TypeSafe (one extra round trip per request,
    which is accepted since the fallback still answers). ``task`` is what
    the LLM usage metrics count the
    answer as (ml/llm_usage.py TASKS).

    Raises TypeSafeError on a non-200, a spent budget or a cooldown, and
    lets aiohttp/asyncio errors propagate: callers decide what a failure
    means for their feature.
    """
    premium_model = model_name()
    opencode_token = getattr(settings, "OPENCODE_TOKEN", None)
    premium_token = getattr(settings, "TYPESAFE_API_KEY", None)
    use_opencode = opencode_route()
    if not use_opencode and not spend.allows(spend.TYPESAFE, use):
        _log_budget_spent(use)
        raise TypeSafeBudgetSpent("budget spent")
    problem = settings_problem()
    if problem:
        # Refused here, before anything is built or sent.
        raise TypeSafeError(problem)
    # OpenCode serves only the public base Jev model. A configured model that
    # differs from our pinned base is a fine-tune/alias and must go directly
    # to TypeSafe, which owns that model namespace.
    if not use_opencode:
        _refuse_while_cooling()
    attempts: list[tuple[str, str, str, bool]] = []
    if use_opencode:
        attempts.append((OPENCODE_API_URL, str(opencode_token), OPENCODE_MODEL, False))
    premium_allowed = spend.allows(spend.TYPESAFE, use) and (
        not use_opencode or _cooldown[0] <= time.monotonic()
    )
    if premium_token and premium_allowed:
        attempts.append(
            (
                str(getattr(settings, "TYPESAFE_API_URL", "") or ""),
                str(premium_token),
                premium_model,
                True,
            )
        )
    if not attempts:
        raise TypeSafeError(NO_KEY)
    # The connect bound is kept well inside the total and inside a caller's
    # own wait_for of the same length (the chat gate's), so a host that
    # cannot be reached fails here as a connect timeout, which counts toward
    # the cooldown, rather than being cut off by the caller's cancellation.
    # `connect` bounds the whole connect phase once (DNS, every address
    # aiohttp tries, TCP and TLS); when it expires, aiohttp (3.14) raises
    # ConnectionTimeoutError, as it wraps the timeout of connector.connect().
    # `sock_connect` alone bounds one address round, and api.typesafe.ai has
    # two addresses: the second round then ran past `total` and failed as a
    # plain TimeoutError, which never counts toward the cooldown.
    connect_bound = min(timeout_seconds * 0.75, CONNECT_TIMEOUT_SECONDS)
    last_error: typing.Optional[BaseException] = None
    payload: typing.Any = None
    answered_model = premium_model
    for url, token, model, is_premium in attempts:
        # The free attempt gets half of the caller's budget, so the premium
        # fallback that follows it still has time inside a caller's own
        # wait_for of the same length: a hung free endpoint must not eat the
        # whole wait and starve the fallback.
        attempt_total = timeout_seconds if is_premium else timeout_seconds * 0.5
        attempt_connect = min(connect_bound, attempt_total * 0.75)
        client_timeout = aiohttp.ClientTimeout(
            total=attempt_total, connect=attempt_connect, sock_connect=attempt_connect
        )
        body = {
            "state": state[:STATE_CHAR_CAP],
            "model": model,
            "questions": questions,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if is_premium:
            # The eligibility check above ran before the free attempt, which
            # can eat its whole half of the budget: the spend ledger and the
            # cooldown can have turned while it ran.
            if not spend.allows(spend.TYPESAFE, use):
                _log_budget_spent(use)
                raise TypeSafeBudgetSpent("budget spent")
            _refuse_while_cooling()
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                # No redirects: a 307 or 308 toward http would make aiohttp
                # resend the state in the clear.
                async with session.post(
                    url, json=body, headers=headers, allow_redirects=False
                ) as response:
                    if is_premium:
                        # Only a premium answer shows the premium endpoint's
                        # connect works: a free answer between premium connect
                        # timeouts must not clear that streak.
                        _clear_connect_timeouts()
                    if response.status != 200:
                        if is_premium:
                            await _note_refusal(response)
                        raise TypeSafeError(
                            f"HTTP {response.status}", status=response.status
                        )
                    if is_premium:
                        _end_cooldown()
                    payload = await response.json()
                    answered_model = model
                    break
        except (
            TypeSafeError,
            aiohttp.ClientError,
            TimeoutError,
            OSError,
            ValueError,
        ) as e:
            last_error = e
            if not is_premium and premium_token:
                logger.warning(
                    f"OpenCode Jev unavailable ({type(e).__name__}); "
                    "falling through to TypeSafe"
                )
                continue
            if is_premium and isinstance(e, aiohttp.ClientConnectorError):
                _start_cooldown(
                    f"could not be reached ({type(e).__name__})",
                    None,
                    cap=CONNECT_COOLDOWN_SECONDS,
                )
            elif is_premium and isinstance(e, aiohttp.ConnectionTimeoutError):
                if (
                    connect_bound >= MIN_COUNTED_CONNECT_SECONDS
                    and _connect_timed_out()
                ):
                    _start_cooldown(
                        f"could not be reached ({type(e).__name__}, "
                        f"{CONNECT_TIMEOUT_STREAK} in a row)",
                        None,
                        cap=CONNECT_COOLDOWN_SECONDS,
                    )
            raise
    else:
        assert last_error is not None
        raise last_error
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict):
        # OpenCode's base Jev is free; only the premium TypeSafe answer uses
        # the TypeSafe budget.
        if answered_model != OPENCODE_MODEL:
            spend.record(
                spend.TYPESAFE,
                use,
                spend.typesafe_cost_micro(usage.get("input_tokens")),
            )
    llm_usage.record_llm_usage(
        model=(
            f"opencode/{reported_model(payload, answered_model)}"
            if answered_model == OPENCODE_MODEL
            else f"typesafe/{reported_model(payload, answered_model)}"
        ),
        tier=llm_usage.EXTERNAL,
        usage=usage,
        task=task,
    )
    return payload

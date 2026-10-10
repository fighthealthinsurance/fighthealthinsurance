"""LLM usage: requests and tokens by where they came from, counted privately.

Every answered model request is counted once, with the tokens its provider
reported, along four bounded dimensions:

* ``surface``: where the work entered. ``site`` (patient pages and
  sockets), ``pro`` (professional chat, prior auth, a professional's case),
  ``assistant`` (a case an AI assistant brought in: Denial.channel
  "assistant", or a latest ConsentRecord of channel "assistant"), ``staff``
  (/timbit tools), ``system`` (no person behind it: probes, the chooser,
  prefetch), ``unknown`` (no entry point set one: a wiring gap, shown on
  purpose so it can be fixed).
* ``task``: what the model was asked to do (TASKS).
* ``tier``: ``internal`` (our own models) or ``external``.
* ``network_class``: the client's network (client_network.NETWORK_CLASSES).

The entry points set an Origin (surface and network) and the flows set a
task, both in ContextVars, like ML_CALL_PURPOSE: the executors in exec.py
copy the context, so they reach the model threads. Work that crosses into
Ray or Temporal loses the context and sets it again from the Denial or chat
it works on (origin_for_denial, origin_for_chat), without the address.

Prometheus gets the bounded labels only. The database (llm_usage_ledger.py)
also gets each day's ASN name and country, and a weekly keyed digest of the
client's /24 (IPv4) or /48 (IPv6), kept only until its week is rolled up.
Never stored anywhere: the address or its prefix, user, denial or chat ids,
or any text. See docs/llm-usage-metrics.md.

All recording is no-op safe: a metrics failure must never break a call.
"""

import contextlib
import contextvars
import dataclasses
import datetime
import functools
import inspect
import math
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
)

from django.conf import settings
from loguru import logger
from prometheus_client import Counter

from fighthealthinsurance import client_network
from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_metrics import ML_CALL_PURPOSE

# --- the dimensions ---------------------------------------------------------

SITE = "site"
PRO = "pro"
ASSISTANT = "assistant"
STAFF = "staff"
SYSTEM = "system"
UNKNOWN = "unknown"
SURFACES = (SITE, PRO, ASSISTANT, STAFF, SYSTEM, UNKNOWN)
# Surfaces a person's own network is behind, so the only ones keyed.
PERSON_SURFACES = frozenset({SITE, PRO, ASSISTANT})

INTERNAL = "internal"
EXTERNAL = "external"
TIERS = (INTERNAL, EXTERNAL)

TASKS = (
    # Letters.
    "appeal_letter",
    "appeal_precompute",
    "synthesis",
    "prior_auth_letter",
    "regulator_letter",
    # Chat.
    "chat_reply",
    "chat_summary",
    "chat_analysis",
    # Reading and research.
    "entity_extraction",
    "questions",
    "citations",
    "doc_summary",
    "policy_analysis",
    "pubmed_summary",
    # TypeSafe (Jev) checks.
    "triage",
    "letter_scoring",
    "reply_gate",
    "reply_shadow",
    # Everything else.
    "chooser",
    "staff_query",
    "intro_email",
    "probe",
    "other",
)
_TASKS = frozenset(TASKS)

# What a call's ML_CALL_PURPOSE means when no flow named a task.
_PURPOSE_TASK = {"appeal": "appeal_letter", "chat": "chat_reply", "probe": "probe"}

# The keyed rows' sub-key of SECRET_KEY (client_network.period_key).
NETWORK_KEY_LABEL = b"fhi-llm-usage-network-week-v1"
# The prefix a client's network is keyed by.
V4_BITS = 24
V6_BITS = 48

_ASN_CHARS = 80
_TOKEN_CAP = 10_000_000


def _surface_label(value: object) -> str:
    return value if isinstance(value, str) and value in SURFACES else UNKNOWN


def _task_label(value: object) -> str:
    return value if isinstance(value, str) and value in _TASKS else "other"


def _class_label(value: object) -> str:
    return (
        value
        if isinstance(value, str) and value in client_network.NETWORK_CLASSES
        else client_network.UNKNOWN
    )


def _model_label(value: object) -> str:
    return str(value)[:80] if value else "unknown"


_SPACES = re.compile(r"\s+")


def _asn_label(value: object) -> str:
    return _SPACES.sub(" ", str(value or "")).strip()[:_ASN_CHARS]


# --- the origin ---------------------------------------------------------------


@dataclass(frozen=True)
class Origin:
    """Where the work in this task came from. ``prefix`` is the client's /24
    or /48, held in memory only to key its week's row; it is never stored,
    logged or passed to another process."""

    surface: str
    network_class: str = client_network.NONE
    asn_name: str = ""
    country: str = ""
    prefix: Optional[str] = field(default=None, repr=False, compare=False)

    def with_surface(self, surface: str) -> "Origin":
        return dataclasses.replace(self, surface=_surface_label(surface))


_SYSTEM_ORIGIN = Origin(SYSTEM)
_STAFF_ORIGIN = Origin(STAFF)
_UNKNOWN_ORIGIN = Origin(UNKNOWN, network_class=client_network.UNKNOWN)

_ORIGIN: contextvars.ContextVar[Optional[Origin]] = contextvars.ContextVar(
    "fhi_llm_origin", default=None
)


@dataclass(frozen=True)
class _TaskState:
    # A pinned task wins over everything inside it (the chooser's synthesis
    # and chat calls are the chooser's); a step is the innermost flow's.
    pin: Optional[str] = None
    step: Optional[str] = None


_TASK: contextvars.ContextVar[_TaskState] = contextvars.ContextVar(
    "fhi_llm_task", default=_TaskState()
)


def _reset(var: contextvars.ContextVar, token: contextvars.Token) -> None:
    try:
        var.reset(token)
    except ValueError:
        # Reset from another context (an async generator finalized
        # elsewhere): that context is gone, and so is what it set.
        pass


def current_origin() -> Optional[Origin]:
    return _ORIGIN.get()


@contextlib.contextmanager
def origin(value: Optional[Origin]) -> Iterator[None]:
    """Count the model calls made inside this block as from ``value``."""
    token = _ORIGIN.set(value)
    try:
        yield
    finally:
        _reset(_ORIGIN, token)


@contextlib.contextmanager
def origin_scope() -> Iterator[None]:
    """A block whose set_surface() / note_denial() calls are undone at its
    end (like spend.channel_scope)."""
    token = _ORIGIN.set(_ORIGIN.get())
    try:
        yield
    finally:
        _reset(_ORIGIN, token)


def set_surface(surface: str) -> None:
    """Refine the rest of this task's origin to ``surface``. Inside an
    origin() or origin_scope() block, which undoes it."""
    current = _ORIGIN.get()
    if current is None:
        current = _UNKNOWN_ORIGIN
    _ORIGIN.set(current.with_surface(surface))


@contextlib.contextmanager
def llm_task(task: Optional[str], *, pin: bool = False) -> Iterator[None]:
    """Count the model calls made inside this block as ``task``. A pinned
    task holds for everything inside it; otherwise an inner task wins."""
    if not task:
        yield
        return
    label = _task_label(task)
    state = _TASK.get()
    if pin and state.pin is None:
        new = _TaskState(pin=label, step=state.step)
    else:
        new = _TaskState(pin=state.pin, step=label)
    token = _TASK.set(new)
    try:
        yield
    finally:
        _reset(_TASK, token)


_F = TypeVar("_F", bound=Callable[..., Any])


def labelled_task(task: str, *, pin: bool = False) -> Callable[[_F], _F]:
    """Decorate an async function so the model calls made inside it count
    as ``task``. Not for async generators: a ContextVar set inside one
    leaks into its caller between items."""

    def decorate(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with llm_task(task, pin=pin):
                return await fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


@contextlib.contextmanager
def system_work(task: Optional[str] = None) -> Iterator[None]:
    """Work with no person behind it (the chooser, prefetch), its task
    pinned when given."""
    with origin(_SYSTEM_ORIGIN), llm_task(task, pin=True):
        yield


@contextlib.contextmanager
def staff_work(task: Optional[str] = None) -> Iterator[None]:
    """A /timbit staff tool. Staff networks are never keyed."""
    with origin(_STAFF_ORIGIN), llm_task(task, pin=True):
        yield


# --- building an origin ---------------------------------------------------------


def _network_origin(surface: str, ip: str, cf_country: str) -> Origin:
    surface = _surface_label(surface)
    prefix = client_network.prefix_of(ip, v4_bits=V4_BITS, v6_bits=V6_BITS)
    if prefix == client_network.NO_ADDRESS:
        # No address Cloudflare vouched for: a request inside the cluster,
        # local dev, or a header someone else set. Nothing to name or key.
        return Origin(surface, network_class=client_network.UNKNOWN)
    from fhi_users.audit import peek_network_info

    asn_name, geo_country = peek_network_info(ip)
    network_class = client_network.classify(ip, asn_name, cf_country)
    if network_class == client_network.TOR:
        country = ""
    else:
        country = client_network.country_label(cf_country) or geo_country
    return Origin(
        surface,
        network_class=network_class,
        asn_name=_asn_label(asn_name),
        country=client_network.country_label(country),
        prefix=prefix,
    )


def origin_from_meta(meta: Mapping[str, Any], surface: str = SITE) -> Origin:
    return _network_origin(
        surface,
        client_network.cf_ip_from_meta(meta),
        client_network.cf_country_from_meta(meta),
    )


def origin_from_request(request: Any, surface: str = SITE) -> Origin:
    """The origin of a Django or DRF request. Never raises."""
    try:
        return origin_from_meta(getattr(request, "META", None) or {}, surface)
    except Exception:
        logger.opt(exception=True).debug("LLM usage origin not read from request")
        return Origin(_surface_label(surface), network_class=client_network.UNKNOWN)


def origin_from_scope(
    scope: Optional[Mapping[str, Any]], surface: str = SITE
) -> Origin:
    """The origin of an ASGI (WebSocket) connection. Never raises."""
    try:
        return _network_origin(
            surface,
            client_network.cf_ip_from_scope(scope),
            client_network.cf_country_from_scope(scope),
        )
    except Exception:
        logger.opt(exception=True).debug("LLM usage origin not read from scope")
        return Origin(_surface_label(surface), network_class=client_network.UNKNOWN)


def _request_in(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    if "request" in kwargs:
        return kwargs["request"]
    return next((a for a in args if hasattr(a, "META")), None)


def http_entry(surface: str = SITE) -> Callable[[_F], _F]:
    """Decorate a view (function or method, sync or async) so the model
    calls it makes count as from its request's origin."""

    def decorate(fn: _F) -> _F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with origin(origin_from_request(_request_in(args, kwargs), surface)):
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with origin(origin_from_request(_request_in(args, kwargs), surface)):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def _professional_case(denial: Any) -> bool:
    return bool(
        getattr(denial, "creating_professional_id", None)
        or getattr(denial, "primary_professional_id", None)
    )


def origin_for_denial(
    current: Optional[Origin], denial: Any, assistant_brought: bool
) -> Origin:
    """The origin of work on ``denial``: assistant when an assistant brought
    the case in; pro for a professional's case (or work entered as pro);
    else site. The network is the entry point's when there is one, else the
    class of the ASN recorded on the denial, unkeyed (no address is kept).
    Staff and system work stays theirs."""
    if current is not None and current.surface in (STAFF, SYSTEM):
        return current
    if assistant_brought or getattr(denial, "channel", None) == ASSISTANT:
        surface = ASSISTANT
    elif (current is not None and current.surface == PRO) or _professional_case(denial):
        surface = PRO
    else:
        surface = SITE
    if current is not None and current.surface != UNKNOWN:
        return current.with_surface(surface)
    asn_name = _asn_label(getattr(denial, "asn_name", ""))
    return Origin(
        surface,
        network_class=client_network.classify_asn(asn_name),
        asn_name=asn_name,
    )


_BROUGHT_ATTR = "_fhi_llm_usage_assistant_brought"


def _channel_is_assistant(denial: Any) -> bool:
    return getattr(denial, "channel", None) == ASSISTANT


def note_denial(denial: Any) -> None:
    """Set the rest of this task's origin from ``denial`` (sync). Inside an
    origin_scope() block. Never raises."""
    try:
        brought = getattr(denial, _BROUGHT_ATTR, None)
        if brought is None:
            if _channel_is_assistant(denial):
                brought = True
            else:
                from fighthealthinsurance import consent

                brought = consent.assistant_that_brought(denial) is not None
            setattr(denial, _BROUGHT_ATTR, brought)
        _ORIGIN.set(origin_for_denial(_ORIGIN.get(), denial, bool(brought)))
    except Exception:
        logger.opt(exception=True).debug("LLM usage origin not set from denial")


async def anote_denial(denial: Any) -> None:
    """note_denial for async code (the consent lookup is native async)."""
    try:
        brought = getattr(denial, _BROUGHT_ATTR, None)
        if brought is None:
            if _channel_is_assistant(denial):
                brought = True
            else:
                from fighthealthinsurance import consent

                brought = await consent.aassistant_that_brought(denial) is not None
            setattr(denial, _BROUGHT_ATTR, brought)
        _ORIGIN.set(origin_for_denial(_ORIGIN.get(), denial, bool(brought)))
    except Exception:
        logger.opt(exception=True).debug("LLM usage origin not set from denial")


def _denial_in(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    denial = kwargs.get("denial")
    if denial is not None:
        return denial
    return next(
        (a for a in args if hasattr(a, "channel") and hasattr(a, "denial_text")),
        None,
    )


def for_denial(task: Optional[str] = None) -> Callable[[_F], _F]:
    """Decorate an async helper that takes the Denial it works on, so its
    model calls count from that denial's origin (and as ``task``). Sits next
    to spend.for_denial_channel; unlike it, never refuses a call."""

    def decorate(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with origin_scope(), llm_task(task):
                denial = _denial_in(args, kwargs)
                if denial is not None:
                    await anote_denial(denial)
                return await fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


_PRO_CHAT_TYPES = frozenset({"professional", "trial_professional"})


def origin_for_chat(chat: Any) -> Origin:
    """The origin of background work on an OngoingChat, unkeyed."""
    surface = PRO if getattr(chat, "chat_type", None) in _PRO_CHAT_TYPES else SITE
    asn_name = _asn_label(getattr(chat, "asn_name", ""))
    return Origin(
        surface, network_class=client_network.classify_asn(asn_name), asn_name=asn_name
    )


def surface_for_chat_type(chat_type: object) -> str:
    return PRO if chat_type in _PRO_CHAT_TYPES else SITE


# --- reading usage ------------------------------------------------------------


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return min(max(0, int(value)), _TOKEN_CAP)


def parse_usage(usage: Any) -> Optional[Tuple[int, int]]:
    """``(prompt_tokens, completion_tokens)`` from a response's usage block,
    or None when it has none. Reads the OpenAI shape (prompt_tokens /
    completion_tokens) and the Anthropic one (input_tokens, plus the cache
    reads and writes, which are input too / output_tokens); TypeSafe sends
    input_tokens alone."""
    if not isinstance(usage, Mapping):
        return None
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    if prompt is not None or completion is not None:
        return (prompt or 0, completion or 0)
    given = _count(usage.get("input_tokens"))
    output = _count(usage.get("output_tokens"))
    if given is None and output is None:
        return None
    cached = sum(
        _count(usage.get(name)) or 0
        for name in ("cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return (min((given or 0) + cached, _TOKEN_CAP), output or 0)


# --- recording ------------------------------------------------------------------

# Requests answered, by where they came from. ``usage`` says whether the
# provider reported tokens (reported) or not (missing): a missing-usage
# backend's tokens are absent from the token series, not zero.
LLM_REQUESTS_TOTAL = Counter(
    "fhi_llm_requests_total",
    "Answered model requests by model, tier, surface, task and whether the "
    "provider reported usage.",
    labelnames=("model", "tier", "surface", "task", "usage"),
)
LLM_TOKENS_TOTAL = Counter(
    "fhi_llm_tokens_total",
    "Tokens providers reported, by model, tier, surface, task and kind "
    "(prompt, completion).",
    labelnames=("model", "tier", "surface", "task", "kind"),
)
# The network view, without the model: a class only, never an address, a
# key, an ASN or a country.
LLM_NETWORK_REQUESTS_TOTAL = Counter(
    "fhi_llm_network_requests_total",
    "Answered model requests by tier, surface, task and client network class.",
    labelnames=("tier", "surface", "task", "network_class"),
)
LLM_NETWORK_TOKENS_TOTAL = Counter(
    "fhi_llm_network_tokens_total",
    "Tokens providers reported, by tier, surface, client network class and kind.",
    labelnames=("tier", "surface", "network_class", "kind"),
)


def resolve_task(explicit: Optional[str] = None) -> str:
    """The task a call in this context counts as: a pinned task; else a
    probe; else what the call site says; else the innermost flow's step;
    else what its ML_CALL_PURPOSE means; else other."""
    state = _TASK.get()
    if state.pin:
        return state.pin
    purpose = ML_CALL_PURPOSE.get()
    if purpose == "probe":
        return "probe"
    if explicit:
        return _task_label(explicit)
    if state.step:
        return state.step
    return _PURPOSE_TASK.get(purpose, "other")


def resolve_origin(task: str) -> Origin:
    """The origin a call in this context counts from. No entry point set
    one: system for probes and the chooser, else unknown. A case an
    assistant brought (spend's channel) is assistant whatever the entry
    point said."""
    current = _ORIGIN.get()
    if current is None:
        current = _SYSTEM_ORIGIN if task in ("probe", "chooser") else _UNKNOWN_ORIGIN
    if current.surface in (SITE, PRO, UNKNOWN) and spend.assistant_work():
        current = current.with_surface(ASSISTANT)
    return current


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def record_llm_usage(
    *, model: object, tier: str, usage: Any, task: Optional[str] = None
) -> None:
    """Count one answered request and the tokens it reported. ``model`` is
    the registry name the fhi_ml_* series use; ``task`` names the call
    site's task when the transport knows it (TypeSafe). Never raises."""
    try:
        tokens = parse_usage(usage)
        task_label = resolve_task(task)
        where = resolve_origin(task_label)
        surface = _surface_label(where.surface)
        network_class = _class_label(where.network_class)
        tier_label = tier if tier in TIERS else EXTERNAL
        model_label = _model_label(model)
        prompt, completion = tokens or (0, 0)
        LLM_REQUESTS_TOTAL.labels(
            model=model_label,
            tier=tier_label,
            surface=surface,
            task=task_label,
            usage="reported" if tokens else "missing",
        ).inc()
        LLM_NETWORK_REQUESTS_TOTAL.labels(
            tier=tier_label,
            surface=surface,
            task=task_label,
            network_class=network_class,
        ).inc()
        if tokens:
            for kind, amount in (("prompt", prompt), ("completion", completion)):
                LLM_TOKENS_TOTAL.labels(
                    model=model_label,
                    tier=tier_label,
                    surface=surface,
                    task=task_label,
                    kind=kind,
                ).inc(amount)
                LLM_NETWORK_TOKENS_TOTAL.labels(
                    tier=tier_label,
                    surface=surface,
                    network_class=network_class,
                    kind=kind,
                ).inc(amount)
        if not getattr(settings, "FHI_LLM_USAGE_DB", True):
            return
        from fighthealthinsurance.ml import llm_usage_ledger

        day = _today()
        amounts = llm_usage_ledger.Amounts(
            calls=1,
            usage_missing_calls=0 if tokens else 1,
            prompt_tokens=prompt,
            completion_tokens=completion,
        )
        llm_usage_ledger.add_daily(
            day, surface, task_label, model_label, tier_label, network_class, amounts
        )
        if network_class != client_network.NONE:
            llm_usage_ledger.add_network(
                day, surface, network_class, where.asn_name, where.country, amounts
            )
        if (
            where.prefix
            and surface in PERSON_SURFACES
            and getattr(settings, "FHI_LLM_USAGE_NETWORK_KEYS", True)
        ):
            week = client_network.week_start(day)
            key = client_network.period_key(
                NETWORK_KEY_LABEL, week.isoformat(), where.prefix
            )
            llm_usage_ledger.add_week(week, key, surface, network_class, amounts)
    except Exception:  # pragma: no cover - metrics must never break calls
        logger.opt(exception=True).debug("LLM usage not recorded")

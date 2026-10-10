"""Deployment-time model-backend health check.

Once per deployment (after migrations, from the single ``web-actor-launch``
job) an elected leader tests every *enabled* model backend end-to-end with a
tiny, inexpensive prompt ("Reply with exactly: OK") using the exact same
provider clients, model names, credentials, and configuration as real
requests — the instances the :class:`~fighthealthinsurance.ml.ml_router.MLRouter`
registered, falling back to a fresh construction only when the router failed
to register a backend (which the check then flags, since a working-but-
unregistered model would silently vanish from the selection UI and usage
reporting).

Design points:

* **Leader election** rides on :class:`ModelHealthAlertState.try_claim` — a
  single-statement conditional UPDATE against the shared database — keyed by
  the deployment identifier, so exactly one process per deployment runs the
  check and (crucially) at most one consolidated alert email is sent.
* **No retries**, one bounded-timeout attempt per backend, all backends
  probed concurrently — a broken provider can't slow the deploy by more than
  the single per-model timeout.
* **Categorized results** distinguish: not configured, missing credentials,
  client-init failure, auth failure, unknown or retired model, rate limiting,
  credit or quota exhausted (billing: it will not recover on its own),
  timeout, network failure, malformed/empty response, success, and
  success-but-missing-from-registry.
* **Sanitized errors**: provider error text passes through
  :func:`sanitize_error` (which strips anything resembling keys/tokens and any
  configured secret values) before logging, persisting, or emailing. API keys,
  authorization headers, and environment variables are never logged.
* A machine-greppable ``MODEL_BACKEND_HEALTH_SUMMARY`` block is always
  logged, and one row per backend is persisted to
  :class:`ModelBackendHealthCheckResult` for the staff status page.
* Failures **never crash the deploy** unless strict mode
  (``FHI_MODEL_HEALTH_STRICT=1``) is enabled.

Manual runs: ``python manage.py check_model_backends`` (see the management
command for options). Alerting is controlled by
``FHI_MODEL_HEALTH_ALERT_EMAIL`` (default: on outside DEBUG/test
environments; ``1`` forces on, ``0`` forces off).
"""

import asyncio
import itertools
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import List, Optional, Tuple, Type

import aiohttp
from asgiref.sync import async_to_sync
from loguru import logger

from fighthealthinsurance.env_utils import local_dotenv_values
from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_metrics import ml_call_purpose
from fighthealthinsurance.ml.ml_models import (
    ModelDescription,
    RateLimitedRemoteOpenLike,
    RemoteModel,
    RemoteModelLike,
    RetiredEndpointError,
    _error_body_of,
    _http_error_indicates_retired_model,
    begin_probe_observations,
    candidate_model_backends,
)
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.ml.retired_models import retirement

# Where the consolidated failure alert is sent. Matches the on-call alias used
# by the existing model-liveness alerts.
SUPPORT_EMAIL = "support42@fighthealthinsurance.com"

# The tiny prompt used for the end-to-end check. Kept deliberately short so
# each probe costs a handful of tokens.
HEALTH_CHECK_PROMPT = "Reply with exactly: OK"
HEALTH_CHECK_SYSTEM_PROMPT = (
    "You are part of an automated health check. Reply with exactly: OK"
)

# What counts as the model acknowledging the probe: a SHORT reply that
# contains the word "OK" or "okay" and nothing that contradicts it. Instruct
# models decorate ("OK!", "Sure — OK", "Reply: OK"), so an exact match is too
# strict; but the previous rule, any reply containing the word anywhere,
# passed "not ok", "HTTP 200 OK" and "I am unable to reply with only OK as
# instructed", so a backend that could not follow the instruction was
# persisted as PASS.
_OK_WORD_TOKENS = frozenset({"ok", "okay"})
_OK_NEGATION_TOKENS = frozenset(
    {
        "not",
        "no",
        "cannot",
        "can't",
        "cant",
        "unable",
        "won't",
        "wont",
        "don't",
        "dont",
        "never",
        "sorry",
        "refuse",
        "isn't",
        "isnt",
        "nope",
        "false",
    }
)
# "OK, no problem" acknowledges: "no" before one of these is not a negation.
_OK_NO_ACKNOWLEDGES = frozenset({"problem", "problems", "worries"})
_MAX_OK_REPLY_WORDS = 4
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
# A citation marker ("OK[1]", "OK [^2]"), which search-backed models such as
# Perplexity attach to their answers. Its digits are not a status code; one
# or two of them, so a bracketed status ("[200] OK") still fails.
_CITATION_MARKER_RE = re.compile(r"\[\^?\d{1,2}\]")


def _negates(words: list[str], i: int) -> bool:
    """Whether ``words[i]`` contradicts an OK in the same reply."""
    if words[i] not in _OK_NEGATION_TOKENS:
        return False
    following = words[i + 1] if i + 1 < len(words) else ""
    return not (words[i] == "no" and following in _OK_NO_ACKNOWLEDGES)


def _looks_like_ok(text: str) -> bool:
    """Whether a probe reply plausibly acknowledges the 'Reply with exactly:
    OK' instruction: at most a few words, one of them OK/okay, none a
    negation, none a bare number (a status line such as "200 OK"). Citation
    markers are dropped first."""
    text = _CITATION_MARKER_RE.sub(" ", text or "")
    # Quotes around a word are not part of it: 'OK' is OK. An apostrophe
    # inside one ("can't") stays.
    words = [w.strip("'").lower() for w in _WORD_RE.findall(text)]
    words = [w for w in words if w]
    if not words or len(words) > _MAX_OK_REPLY_WORDS:
        return False
    if not any(w in _OK_WORD_TOKENS for w in words):
        return False
    if any(_negates(words, i) for i in range(len(words))):
        return False
    return not any(w.isdigit() for w in words)


# --- Result categories ------------------------------------------------------
CATEGORY_PASS = "PASS"
# The backend answered, but the router never registered it — it would be
# invisible to the selection UI and usage reporting despite working.
CATEGORY_PASS_UNREGISTERED = "PASS_UNREGISTERED"
# Provider intentionally off (none of its configuration present).
CATEGORY_NOT_CONFIGURED = "NOT_CONFIGURED"
# Excluded by the ENABLED_REMOTE_MODELS allow-list; never invoked.
CATEGORY_DISABLED = "DISABLED"
# On the retired list (ml/retired_models.py); never invoked, whatever the settings.
CATEGORY_RETIRED = "RETIRED"
CATEGORY_MISSING_CREDENTIALS = "FAIL_MISSING_CREDENTIALS"
CATEGORY_CLIENT_INIT = "FAIL_CLIENT_INIT"
CATEGORY_AUTH = "FAIL_AUTH"
CATEGORY_MODEL_NOT_FOUND = "FAIL_MODEL_NOT_FOUND"
CATEGORY_RATE_LIMITED = "FAIL_RATE_LIMITED"
# Credit or quota exhausted (HTTP 402, or a quota message in the body). Unlike
# a rate limit it will not recover on its own: someone has to pay or raise
# the limit, so it must not read as a transient FAIL_RATE_LIMITED.
CATEGORY_BILLING = "FAIL_BILLING"
CATEGORY_TIMEOUT = "FAIL_TIMEOUT"
CATEGORY_NETWORK = "FAIL_NETWORK"
CATEGORY_MALFORMED_RESPONSE = "FAIL_MALFORMED_RESPONSE"
CATEGORY_OTHER = "FAIL_OTHER"

# Categories that count as failures for alerting/strict mode. PASS variants
# and the intentionally-off categories are not failures — but
# PASS_UNREGISTERED is called out separately in the summary and email because
# it means users can't see a model that works.
FAILURE_CATEGORIES = frozenset(
    {
        CATEGORY_MISSING_CREDENTIALS,
        CATEGORY_CLIENT_INIT,
        CATEGORY_AUTH,
        CATEGORY_MODEL_NOT_FOUND,
        CATEGORY_RATE_LIMITED,
        CATEGORY_BILLING,
        CATEGORY_TIMEOUT,
        CATEGORY_NETWORK,
        CATEGORY_MALFORMED_RESPONSE,
        CATEGORY_OTHER,
    }
)

DEFAULT_TIMEOUT_SECONDS = 30.0
# One leader claim per deployment id; a re-apply of the same version within
# this window will not rerun (use the manual command instead).
LEADER_CLAIM_WINDOW_SECONDS = 6 * 60 * 60


# --- Sanitization ------------------------------------------------------------

# Env var names whose values are treated as secrets and scrubbed from any
# error text. Matched case-insensitively on the *name*.
_SECRET_ENV_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_API$|_API_)", re.IGNORECASE
)

# "Bearer <token>" (redacted first so the token itself goes, not just the
# scheme word when it follows an Authorization: header).
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s\"',;]+")
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api-key)\b[\"'\s:=]+[^\s\"',;]+"
)
# Provider-style opaque keys (e.g. sk-..., gsk_..., long base64/hex runs).
_KEYLIKE_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|gsk_[A-Za-z0-9_\-]{8,})\b")
_LONG_OPAQUE_RE = re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b")

MAX_SANITIZED_ERROR_LEN = 400


def _secret_env_values() -> List[str]:
    """Values of secret-looking settings, longest first so replacement of a
    long value can't be defeated by a shorter one matching a substring
    earlier.

    Covers the process environment and, on a local run, the repo's .env: the
    backends read their keys through get_env_variable, which falls back to
    .env, so a key that lives only there must still be redacted.
    """
    values = [
        v
        for k, v in itertools.chain(os.environ.items(), local_dotenv_values().items())
        if v and len(v) >= 8 and _SECRET_ENV_NAME_RE.search(k)
    ]
    return sorted(set(values), key=len, reverse=True)


def sanitize_error(message: Optional[str]) -> str:
    """Redact anything secret-shaped from provider error text.

    Removes: the values of secret-looking environment variables (API keys and
    friends), Authorization/x-api-key style header values, provider key
    formats (``sk-…``), and long opaque token-like runs. Output is whitespace
    collapsed and length-capped, safe to log, persist, and email.
    """
    if not message:
        return ""
    text = str(message)
    for value in _secret_env_values():
        if value in text:
            text = text.replace(value, "[REDACTED]")
    text = _BEARER_RE.sub("[REDACTED]", text)
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}: [REDACTED]", text)
    text = _KEYLIKE_RE.sub("[REDACTED]", text)
    text = _LONG_OPAQUE_RE.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SANITIZED_ERROR_LEN:
        text = text[: MAX_SANITIZED_ERROR_LEN - 1] + "…"
    return text


# --- Result plumbing ---------------------------------------------------------


@dataclass
class BackendCheckResult:
    """Outcome of checking one (provider, model) configuration."""

    provider: str
    model_name: str  # friendly registry name (matches usage reporting)
    internal_name: str  # wire-level model id / deployment name
    category: str
    enabled: bool = True
    ok: bool = False
    error: str = ""  # sanitized
    latency_ms: Optional[int] = None
    ui_registered: bool = False
    reporting_registered: bool = False
    # Reserved for building context (citations); never a generation candidate,
    # so it can never produce a stored draft or a chooser candidate.
    context_only: bool = False
    # Served to chat only (the backend's chat_models(), the router's
    # chat_outside_models_by_name), outside every general pool, so it can
    # never produce a stored draft either. Not persisted.
    chat_only: bool = False
    started_at: Optional[datetime] = None
    # Not persisted. The staff status page reads the model's routing traits
    # from these: the instance the router registered, or, when there is none,
    # the backend class, so it can still say what the model would be.
    backend_cls: Optional[Type[RemoteModel]] = field(
        default=None, repr=False, compare=False
    )
    router_instance: Optional[RemoteModelLike] = field(
        default=None, repr=False, compare=False
    )

    @property
    def failed(self) -> bool:
        return self.category in FAILURE_CATEGORIES


@dataclass
class HealthCheckRunSummary:
    """Everything a caller (deploy hook, management command) needs to report."""

    run_id: str
    deployment_id: str
    environment: str
    results: List[BackendCheckResult] = field(default_factory=list)
    ran_checks: bool = True  # False when a non-leader skipped the run
    # True when ran_checks is False because the check itself raised, as
    # opposed to a lost leader claim: the deploy hook fails a strict deploy
    # on the former and exits quietly on the latter.
    crashed: bool = False
    # True when ran_checks is False because the leader claim itself failed
    # (a database error, or the schema not migrated yet): nothing ran, and
    # no other process is known to have run it either. Treated as a crash.
    claim_failed: bool = False
    email_sent: bool = False
    persisted: bool = False

    @property
    def failures(self) -> List[BackendCheckResult]:
        return [r for r in self.results if r.failed]

    @property
    def unregistered_passes(self) -> List[BackendCheckResult]:
        return [r for r in self.results if r.category == CATEGORY_PASS_UNREGISTERED]


# Prefix of the deployment id used when no release variable is set.
_UNVERSIONED_PREFIX = "unversioned-"
# What k8s/Dockerfile bakes into FHI_RELEASE when an image is built without a
# RELEASE build arg: set, but naming no release.
_UNSET_RELEASE = "unknown"


def deployment_id() -> str:
    """Deployment/version identifier for this process.

    Checks ``FHI_DEPLOYMENT_ID``, then ``FHI_RELEASE`` (baked into the image
    from the build arg), then ``FHI_VERSION``. Falls back to a coarse UTC
    hour stamp so unversioned environments still get a usable leader-claim
    key (deduping reruns within the hour).
    """
    for var in ("FHI_DEPLOYMENT_ID", "FHI_RELEASE", "FHI_VERSION"):
        value = os.getenv(var)
        # The image's build arg defaults FHI_RELEASE to "unknown"; treating
        # that as an identifier made every such deploy share one leader slot.
        if value and value.strip() and value.strip().lower() != "unknown":
            return value.strip()
    return _UNVERSIONED_PREFIX + datetime.now(dt_timezone.utc).strftime("%Y%m%d%H")


def is_versioned_deployment_id(value: str) -> bool:
    """Whether ``value`` names a real release rather than the hourly
    fallback stamp or the Dockerfile's ``unknown`` placeholder.

    Only a real release id says which deploy a health row belongs to. The
    fallback changes every hour, so comparing it would call every row stale;
    the placeholder is the same for every image built without a release, so
    comparing it would call none stale.
    """
    return not value.startswith(_UNVERSIONED_PREFIX) and value != _UNSET_RELEASE


def environment_name() -> str:
    return os.getenv("DJANGO_CONFIGURATION") or os.getenv("ENVIRONMENT") or "unknown"


_TRUTHY_FLAGS = frozenset({"1", "true", "yes", "on"})
_FALSY_FLAGS = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str) -> Optional[bool]:
    """True/False for a recognised boolean spelling of ``name``, else None."""
    value = os.getenv(name, "").strip().lower()
    if value in _TRUTHY_FLAGS:
        return True
    if value in _FALSY_FLAGS:
        return False
    return None


def strict_mode_enabled() -> bool:
    """Whether a failed backend should fail the deployment (default: no).

    start-server.sh, which fails the deploy job, honours only ``1``, the
    documented setting; another true spelling makes this command exit 2 and
    the script then reports it as non-blocking.
    """
    return _env_flag("FHI_MODEL_HEALTH_STRICT") is True


def alert_emails_enabled() -> bool:
    """Whether the consolidated failure alert email may be sent.

    ``FHI_MODEL_HEALTH_ALERT_EMAIL=1`` (or true/yes/on) forces on (even in
    dev/test), ``FHI_MODEL_HEALTH_ALERT_EMAIL=0`` (or false/no/off) forces
    off (``_env_flag``, which strict mode reads too); a "false" here used to
    be ignored and keep emailing. Otherwise alerts are disabled in test runs
    (``TESTING=True``) and DEBUG (local dev) environments, and enabled
    elsewhere (production).
    """
    override = _env_flag("FHI_MODEL_HEALTH_ALERT_EMAIL")
    if override is not None:
        return override
    if os.getenv("TESTING") == "True":
        return False
    try:
        from django.conf import settings

        if settings.DEBUG:
            return False
    except Exception:  # pragma: no cover - unconfigured Django
        pass
    return True


# --- Enumeration -------------------------------------------------------------


def _router():
    return ml_router_module.ml_router


def _registered_instance(
    backend_cls: Type[RemoteModel], desc: ModelDescription
) -> Optional[RemoteModelLike]:
    """The router-registered instance for ``desc``, if registration succeeded.

    Using the registered singleton means the check exercises exactly the
    client object real requests use (same credentials, endpoints, rate-limit
    state, dual-mode fan-out).
    """
    try:
        instances = _router().models_by_name.get(desc.name, [])
    except Exception:
        logger.opt(exception=True).warning(
            "Could not consult ml_router.models_by_name; treating "
            f"{desc.name} as unregistered"
        )
        return None
    for instance in instances:
        if isinstance(instance, backend_cls) and (
            getattr(instance, "model", None) in (desc.internal_name, None)
        ):
            return instance
    return None


def _registered_chat_instance(
    backend_cls: Type[RemoteModel], desc: ModelDescription
) -> Optional[RemoteModelLike]:
    """The instance the router registered for a chat-only model
    (``chat_outside_models_by_name``), if it built one."""
    try:
        instance = _router().chat_outside_models_by_name.get(desc.name)
    except Exception:
        logger.opt(exception=True).warning(
            "Could not consult ml_router.chat_outside_models_by_name; treating "
            f"{desc.name} as unregistered"
        )
        return None
    return instance if isinstance(instance, backend_cls) else None


def _registry_flags(
    desc: ModelDescription, instance: Optional[RemoteModelLike]
) -> Tuple[bool, bool, bool]:
    """(ui_registered, reporting_registered, context_only) for a description.

    * ``ui_registered``: the registered instance is in the pool the router
      draws from for its kind of work: the generation pools for a generation
      model (what the chooser and the appeal flows select from), the
      context-only pool for a context-only one (citations). The two used to
      be lumped together, so a citations-only backend read as "in selection
      UI: yes" although nothing can ever select it.
    * ``reporting_registered``: the friendly name is present in
      ``models_by_name`` — the name-stamping registry that usage reporting
      (ProposedAppeal.model_name / ChooserCandidate.model_name) records.
    * ``context_only``: the instance's own flag, so the status page can say
      "context only" instead of "none recorded" for its generations.
    """
    try:
        router = _router()
        reporting = bool(router.models_by_name.get(desc.name))
        ui = False
        context_only = False
        if instance is not None:
            context_only = bool(getattr(instance, "context_only", False))
            pool = (
                router.context_only_models_by_cost
                if context_only
                else router.all_models_by_cost
            )
            ui = id(instance) in {id(m) for m in pool}
        return ui, reporting, context_only
    except Exception:
        logger.opt(exception=True).warning("Could not compute registry flags")
        return False, False, False


def enumerate_backend_checks(
    only_models: Optional[List[str]] = None,
) -> Tuple[List[BackendCheckResult], List[Tuple[BackendCheckResult, RemoteModelLike]]]:
    """Walk every candidate backend class and classify its configuration.

    Returns ``(static_results, checkable)``:

    * ``static_results`` — rows that are decided without any network call:
      retired models, not-configured providers, allow-list-disabled models,
      missing credentials, and client-construction failures.
    * ``checkable`` — ``(pending_result, instance)`` pairs for enabled,
      constructable backends that should actually be invoked.

    A backend's chat-only models (``chat_models()``) get rows too, tagged
    ``chat_only``, checked through the instance the router's chat roster
    holds.

    ``only_models`` (friendly or internal names, case-sensitive) restricts
    enumeration for the manual single-model mode.
    """
    static_results: List[BackendCheckResult] = []
    checkable: List[Tuple[BackendCheckResult, RemoteModelLike]] = []
    enabled_names = MLRouter._enabled_model_names()

    def _wanted(desc: ModelDescription) -> bool:
        if not only_models:
            return True
        return desc.name in only_models or desc.internal_name in only_models

    def _not_allowed(desc: ModelDescription) -> bool:
        return (
            enabled_names is not None
            and desc.name not in enabled_names
            and desc.internal_name not in enabled_names
        )

    def _classify(
        backend_cls: Type[RemoteModel],
        provider: str,
        status: str,
        detail: Optional[str],
        desc: ModelDescription,
        chat_only: bool,
    ) -> None:
        base = BackendCheckResult(
            provider=provider,
            model_name=desc.name,
            internal_name=desc.internal_name,
            category=CATEGORY_OTHER,
            backend_cls=backend_cls,
            chat_only=chat_only,
        )

        retired = retirement(desc.name, desc.internal_name)
        if retired is not None:
            base.category = CATEGORY_RETIRED
            base.enabled = False
            base.error = retired.describe()
            static_results.append(base)
            return
        if status == "not_configured":
            base.category = CATEGORY_NOT_CONFIGURED
            base.enabled = False
            base.error = sanitize_error(detail)
            static_results.append(base)
            return
        if status == "missing_credentials":
            base.category = CATEGORY_MISSING_CREDENTIALS
            base.error = sanitize_error(detail)
            static_results.append(base)
            return

        if chat_only:
            # The router keeps these in its chat roster, outside every general
            # pool, and builds one only when the allow-list names it
            # (MLRouter._register_chat_outside_models). Registered there is
            # all "registered" means for them, so one that answers is a PASS.
            if _not_allowed(desc):
                base.category = CATEGORY_DISABLED
                base.enabled = False
                base.error = "excluded by ENABLED_REMOTE_MODELS"
                static_results.append(base)
                return
            instance = _registered_chat_instance(backend_cls, desc)
            base.ui_registered = base.reporting_registered = instance is not None
        else:
            instance = _registered_instance(backend_cls, desc)
            base.ui_registered, base.reporting_registered, base.context_only = (
                _registry_flags(desc, instance)
            )
        base.router_instance = instance

        probe_instance: Optional[RemoteModelLike] = instance
        if probe_instance is None:
            try:
                probe_instance = backend_cls(model=desc.internal_name)
            except RetiredEndpointError as e:
                # Every endpoint it was given serves a retired model: the
                # operator's own retirement, shown like one, not a failure
                # that pages support and fails a strict deploy. Unsanitized,
                # like the other retirement details: it names only models,
                # which the sanitizer would redact.
                base.category = CATEGORY_RETIRED
                base.enabled = False
                base.error = str(e)
                static_results.append(base)
                return
            except EnvironmentError as e:
                base.category = CATEGORY_MISSING_CREDENTIALS
                base.error = sanitize_error(str(e))
                static_results.append(base)
                return
            except Exception as e:
                base.category = CATEGORY_CLIENT_INIT
                base.error = sanitize_error(f"{type(e).__name__}: {e}")
                static_results.append(base)
                return

        # The ENABLED_REMOTE_MODELS allow-list only gates remote
        # generation models (mirrors MLRouter registration).
        if (
            not chat_only
            and probe_instance.external
            and not probe_instance.context_only
            and _not_allowed(desc)
        ):
            base.category = CATEGORY_DISABLED
            base.enabled = False
            base.error = "excluded by ENABLED_REMOTE_MODELS"
            static_results.append(base)
            return

        checkable.append((base, probe_instance))

    for backend_cls in candidate_model_backends:
        try:
            catalog = backend_cls.model_catalog()
        except Exception as e:
            logger.opt(exception=True).warning(
                f"model_catalog() failed for {backend_cls.__name__}: {e}"
            )
            if only_models and backend_cls.__name__ not in only_models:
                # A check of other models: without a catalog there is no
                # telling whether this class serves them, and its failure
                # row would fail that check and hide a filter that matched
                # nothing. The warning above still says what happened.
                continue
            # A provider whose catalog cannot even be listed vanished from
            # the report (and from the router) without a row; give it one so
            # the failure is visible where the others are.
            provider = backend_cls.provider_label()
            # No backend_cls: with no catalog entry there is no model for the
            # status page to read traits off, and a stand-in of a class whose
            # catalog raises could raise there too.
            static_results.append(
                BackendCheckResult(
                    provider=provider,
                    model_name=backend_cls.__name__,
                    internal_name="",
                    category=CATEGORY_CLIENT_INIT,
                    error=sanitize_error(f"model_catalog() failed: {e}"),
                )
            )
            continue
        # The outside models the backend serves to chat only. They are in no
        # catalog, so without these the deploy check never probed them and
        # the status page had no row for them, though they are the models
        # most likely to be retired or refused under us.
        try:
            chat_catalog = backend_cls.chat_models()
        except Exception as e:
            logger.opt(exception=True).warning(
                f"chat_models() failed for {backend_cls.__name__}: {e}"
            )
            chat_catalog = []
        if not catalog and not chat_catalog:
            continue  # abstract/intermediate class or nothing to expose

        provider = backend_cls.provider_label()
        try:
            status, detail = backend_cls.config_status()
        except Exception as e:
            status, detail = ("configured", None)
            logger.opt(exception=True).warning(
                f"config_status() failed for {backend_cls.__name__}: {e}"
            )

        for desc in catalog:
            if _wanted(desc):
                _classify(backend_cls, provider, status, detail, desc, False)
        cataloged = {d.name for d in catalog} | {d.internal_name for d in catalog}
        for desc in chat_catalog:
            if _wanted(desc) and not (
                desc.name in cataloged or desc.internal_name in cataloged
            ):
                _classify(backend_cls, provider, status, detail, desc, True)

    return static_results, checkable


# --- Invocation --------------------------------------------------------------


def _consume_transport_error(observations: Optional[dict]) -> str:
    """The transport failure this probe observed, or "".

    ``observations`` is the dict handed back by ``begin_probe_observations``:
    private to this probe, so a concurrent production request through the same
    backend instance cannot write into it.
    """
    if not observations:
        return ""
    # One _infer can try several endpoints. A note proves SOME leg could not
    # reach its endpoint; it does not prove none of them did. If any leg got an
    # HTTP response, the empty result came from a backend that answered, which
    # is a malformed response -- calling it "no endpoint reachable" would send
    # the operator to the network layer for a model fault.
    if observations.get("endpoint_answered"):
        return ""
    note = observations.get("transport_error")
    return str(note) if note else ""


def _categorize_http_error(e: aiohttp.ClientResponseError) -> Tuple[str, str]:
    """Categorize an HTTP failure, reporting what the PROVIDER actually said.

    ``e.message`` is only the HTTP reason phrase ("Bad Request"). The transport
    already reads the response body and attaches it (``_attach_error_body``),
    so the operator-facing detail here uses that body: the difference is
    "HTTP 400 Bad Request" versus "Your credit balance is too low to access
    the Anthropic API" -- the first is unactionable, the second names the fix.
    A real incident was diagnosed by hand for exactly this reason.

    The body also decides the category. A missing or retired model is read
    with ``_http_error_indicates_retired_model``, the test the transport uses
    to park the model, so the deploy check and runtime agree: 410, or a
    400/404 naming a missing, retired or invalid model, but not one that
    merely mentions a model ("temperature is deprecated for this model").
    A credit or quota refusal is read with ``spend.quota_refusal``, the test
    that pauses the provider.
    """
    body = _error_body_of(e)
    reason = e.message or ""
    # Body first: it is the part a human acts on. sanitize_error() redacts and
    # length-caps, so a huge HTML error page cannot reach the report whole.
    detail = sanitize_error(
        f"HTTP {e.status} {reason}" + (f" -- {body}" if body else "")
    )
    # Before auth and rate limits: Perplexity says insufficient_quota with a
    # 401 and OpenAI with a 429, and both point at billing, not the key or a
    # wait.
    if spend.quota_refusal(e.status, body):
        return CATEGORY_BILLING, detail
    if e.status in (401, 403):
        return CATEGORY_AUTH, detail
    # Before the 404 fallback below, so a retirement is never sent to the
    # endpoint path.
    if _http_error_indicates_retired_model(e.status, body):
        return CATEGORY_MODEL_NOT_FOUND, detail
    if e.status == 404:
        # A 404 whose body says nothing about the model (vLLM's {"detail":
        # "Not Found"}, Azure's bare "Resource not found") is a wrong base
        # URL: filing it as a missing model sent the operator to the
        # deployment name instead of the endpoint path. No body at all stays
        # a missing model, the common case for a bare 404 from a
        # model-serving endpoint.
        if not body:
            return CATEGORY_MODEL_NOT_FOUND, detail
        return (
            CATEGORY_OTHER,
            f"{detail} (404 without a model error: check the endpoint path)",
        )
    if e.status == 429:
        return CATEGORY_RATE_LIMITED, detail
    if 500 <= e.status < 600:
        return CATEGORY_NETWORK, detail
    return CATEGORY_OTHER, detail


async def check_backend(
    result: BackendCheckResult,
    instance: RemoteModelLike,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> BackendCheckResult:
    """Invoke one backend once with the tiny prompt and categorize the outcome.

    Single attempt, bounded by ``timeout`` — deliberately no retries so a
    degraded provider cannot slow or inflate the cost of a deployment.
    """
    result.started_at = datetime.now(dt_timezone.utc)

    # A provider currently backing off from a 429 would return None without
    # calling the network; report that as rate-limited rather than malformed.
    if isinstance(instance, RateLimitedRemoteOpenLike):
        try:
            if not instance.rate_limiter.can_request():
                result.category = CATEGORY_RATE_LIMITED
                result.error = "provider in rate-limit back-off"
                return result
        except Exception:  # rate limiter not initialized — proceed to probe
            pass

    # Record this probe's transport observations somewhere only this probe can
    # see, so concurrent production traffic through the same shared backend
    # instance cannot change the verdict.
    observations = begin_probe_observations()
    start = time.monotonic()
    try:
        # A probe, and labelled one, so its "Hello"s and failures stay out of
        # the series real traffic is judged by (RemoteModelLike.probe, the
        # startup probe, is labelled the same way).
        with ml_call_purpose("probe"):
            text = await asyncio.wait_for(
                instance._infer_no_context(
                    system_prompts=[HEALTH_CHECK_SYSTEM_PROMPT],
                    prompt=HEALTH_CHECK_PROMPT,
                    raise_http_errors=True,
                ),
                timeout=timeout,
            )
    except asyncio.TimeoutError:
        result.category = CATEGORY_TIMEOUT
        result.error = f"timeout>{timeout:g}s"
        return result
    except aiohttp.ClientResponseError as e:
        result.latency_ms = int((time.monotonic() - start) * 1000)
        result.category, result.error = _categorize_http_error(e)
        return result
    except (aiohttp.ClientError, ConnectionError, OSError) as e:
        result.category = CATEGORY_NETWORK
        result.error = sanitize_error(f"{type(e).__name__}: {e}")
        return result
    except Exception as e:
        result.category = CATEGORY_OTHER
        result.error = sanitize_error(f"{type(e).__name__}: {e}")
        return result

    result.latency_ms = int((time.monotonic() - start) * 1000)
    if text is None or not str(text).strip():
        # A backend whose every endpoint refused or timed out returns None
        # here rather than raising: the transport swallows MODEL_TRANSPORT
        # errors and falls through to its backup, and when the backup fails
        # too the caller just sees None. Reporting that as "malformed
        # response" points the operator at the model when the truth is that
        # nothing answered the socket. Ask the transport what it last saw.
        transport = _consume_transport_error(observations)
        if transport:
            result.category = CATEGORY_NETWORK
            result.error = sanitize_error(f"no endpoint reachable -- {transport}")
            return result
        result.category = CATEGORY_MALFORMED_RESPONSE
        result.error = "empty or no text in provider response"
        return result
    if not _looks_like_ok(str(text)):
        # The backend answered, but not with anything resembling the "OK" the
        # prompt demanded — e.g. an HTML error page surfaced as text, a stub
        # response, or a model too broken to follow a one-word instruction.
        # A reachable-but-garbled backend is not healthy.
        result.category = CATEGORY_MALFORMED_RESPONSE
        result.error = "unexpected response (no OK acknowledgement): " + sanitize_error(
            str(text)[:120]
        )
        return result

    result.ok = True
    if result.ui_registered and result.reporting_registered:
        result.category = CATEGORY_PASS
    else:
        # The backend works but the router never registered it (or only
        # partially): it would be invisible in the selection UI / reporting.
        result.category = CATEGORY_PASS_UNREGISTERED
        missing = []
        if not result.ui_registered:
            missing.append("selection UI")
        if not result.reporting_registered:
            missing.append("reporting registry")
        result.error = f"works but missing from: {', '.join(missing)}"
    return result


async def run_checks_async(
    only_models: Optional[List[str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[BackendCheckResult]:
    """Enumerate and check all (or ``only_models``) backends concurrently."""
    static_results, checkable = enumerate_backend_checks(only_models=only_models)
    if checkable:
        checked = await asyncio.gather(
            *[
                check_backend(result, instance, timeout)
                for result, instance in checkable
            ]
        )
    else:
        checked = []
    results = static_results + list(checked)
    results.sort(key=lambda r: (r.ok, not r.failed, r.provider, r.model_name))
    return results


# --- Persistence, summary, alerting -----------------------------------------


def _persist_results(summary: HealthCheckRunSummary) -> bool:
    """Write one row per result; never raises (deploys must not break if the
    table is missing, e.g. migrations not applied yet)."""
    try:
        from django.utils import timezone as dj_timezone

        from fighthealthinsurance.models import ModelBackendHealthCheckResult

        rows = [
            ModelBackendHealthCheckResult(
                run_id=summary.run_id,
                deployment_id=summary.deployment_id[:128],
                environment=summary.environment[:64],
                provider=r.provider[:100],
                model_name=r.model_name[:200],
                internal_name=(r.internal_name or "")[:200],
                enabled=r.enabled,
                ok=r.ok,
                category=r.category[:64],
                error=r.error or "",
                latency_ms=r.latency_ms,
                ui_registered=r.ui_registered,
                reporting_registered=r.reporting_registered,
                started_at=r.started_at or dj_timezone.now(),
            )
            for r in summary.results
        ]
        ModelBackendHealthCheckResult.objects.bulk_create(rows)
        return True
    except Exception:
        logger.opt(exception=True).warning(
            "Could not persist model backend health results (are migrations "
            "applied?); continuing without persistence"
        )
        return False


def format_summary_block(summary: HealthCheckRunSummary) -> str:
    """The MODEL_BACKEND_HEALTH_SUMMARY block for the deployment logs."""
    lines = [
        "MODEL_BACKEND_HEALTH_SUMMARY "
        f"run={summary.run_id} deployment={summary.deployment_id} "
        f"env={summary.environment} checked={len(summary.results)} "
        f"failed={len(summary.failures)}"
    ]
    for r in summary.results:
        latency = f", {r.latency_ms} ms" if r.latency_ms is not None else ""
        detail = f", {r.error}" if r.error else ""
        lines.append(
            f"* {r.provider} / {r.model_name} [{r.internal_name}]: "
            f"{r.category}{latency}{detail}"
        )
    return "\n".join(lines)


def _send_consolidated_alert(summary: HealthCheckRunSummary) -> bool:
    """One email covering every failed backend for this run. Never raises."""
    failures = summary.failures
    if not failures:
        return False
    failure_lines = []
    for r in failures:
        failure_lines.append(
            f"- provider: {r.provider}\n"
            f"  model: {r.model_name} (internal: {r.internal_name or 'n/a'})\n"
            f"  category: {r.category}\n"
            f"  error: {r.error or 'n/a'}\n"
            f"  registered for selection UI: {'yes' if r.ui_registered else 'NO'}; "
            f"recognized by reporting: {'yes' if r.reporting_registered else 'NO'}"
        )
    # Kept in a clearly separate section: these backends WORK, they are just
    # invisible to users — labeling them "failing" would misdirect triage.
    unregistered_section = ""
    if summary.unregistered_passes:
        unregistered_lines = [
            f"- provider: {r.provider}\n"
            f"  model: {r.model_name} (internal: {r.internal_name or 'n/a'})\n"
            f"  detail: {r.error or 'n/a'}"
            for r in summary.unregistered_passes
        ]
        unregistered_section = (
            "Working but NOT registered (not failures — these backends "
            "answered the probe but are missing from the selection UI / "
            "reporting registry, so users cannot see them):\n\n"
            + "\n\n".join(unregistered_lines)
            + "\n\n"
        )
    timestamp = datetime.now(dt_timezone.utc).isoformat()
    subject = (
        f"[FHI] Model backend health check: {len(failures)} failing backend(s) "
        f"(deploy {summary.deployment_id})"
    )
    message = (
        "The deployment model-backend health check found problems.\n\n"
        f"Deployment: {summary.deployment_id}\n"
        f"Environment: {summary.environment}\n"
        f"Run id: {summary.run_id}\n"
        f"Timestamp (UTC): {timestamp}\n\n"
        f"Failing backends:\n\n"
        + "\n\n".join(failure_lines)
        + "\n\n"
        + unregistered_section
        + "Each backend was tested once with a tiny 'Reply with exactly: OK' "
        "prompt through the same client/credentials/routing as real requests.\n\n"
        "Where to look:\n"
        "- Deployment logs: search for MODEL_BACKEND_HEALTH_SUMMARY in the "
        "web-actor-launch job output.\n"
        "- Staff dashboard: /timbit/help/model_backends (latest result per "
        "backend).\n"
        "- Re-run manually: python manage.py check_model_backends\n"
    )
    try:
        from django.conf import settings
        from django.core.mail import send_mail

        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [SUPPORT_EMAIL],
            fail_silently=False,
        )
        logger.info(
            f"Sent consolidated model-backend health alert "
            f"({len(failures)} failure(s)) to {SUPPORT_EMAIL}"
        )
        return True
    except Exception:
        logger.opt(exception=True).error(
            "Failed to send model-backend health alert email"
        )
        return False


def try_claim_deployment_leader(deploy_id: str) -> Optional[bool]:
    """Claim the once-per-deployment leader slot via the shared database.

    Exactly one caller across every pod/process sharing the database wins for
    a given deployment id (within ``LEADER_CLAIM_WINDOW_SECONDS``). On any
    database error we return ``None``, which callers must not run the check
    on either — better to occasionally skip the check than to have every
    worker run it and email support in parallel — but must not report as a
    lost claim, since no other process is known to have run it.
    """
    try:
        from django.db import close_old_connections

        from fighthealthinsurance.models import ModelHealthAlertState

        close_old_connections()
        return ModelHealthAlertState.try_claim(
            f"model_backend_health:{deploy_id}"[:64], LEADER_CLAIM_WINDOW_SECONDS
        )
    except Exception:
        logger.opt(exception=True).warning(
            "Model-backend health leader claim unavailable (DB error or "
            "migrations not applied); skipping to avoid duplicate runs"
        )
        return None


def run_health_check(
    *,
    only_models: Optional[List[str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    require_leader: bool = False,
    send_alert_email: bool = False,
    persist: bool = True,
) -> HealthCheckRunSummary:
    """Run the model-backend health check and handle reporting side effects.

    * ``require_leader`` — claim the per-deployment leader slot first; when the
      claim is lost (another process already ran for this deployment) the
      returned summary has ``ran_checks=False`` and nothing is invoked. When
      the claim cannot be made at all, ``claim_failed`` is set too.
    * ``send_alert_email`` — send the single consolidated failure email
      (subject to :func:`alert_emails_enabled`; only meaningful together with
      ``require_leader`` so exactly one email can exist per deployment).
    * ``persist`` — write per-backend rows for the staff status page.

    Never raises; callers inspect the summary (and strict mode) to decide
    exit codes.
    """
    deploy_id = deployment_id()
    summary = HealthCheckRunSummary(
        run_id=uuid.uuid4().hex,
        deployment_id=deploy_id,
        environment=environment_name(),
    )

    if require_leader:
        claimed = try_claim_deployment_leader(deploy_id)
        if claimed is None:
            summary.ran_checks = False
            summary.claim_failed = True
            return summary
        if not claimed:
            logger.info(
                f"Model-backend health check: another process already ran for "
                f"deployment {deploy_id}; skipping"
            )
            summary.ran_checks = False
            return summary

    try:
        summary.results = async_to_sync(run_checks_async)(
            only_models=only_models, timeout=timeout
        )
    except Exception:
        logger.opt(exception=True).error("Model-backend health check failed to run")
        summary.ran_checks = False
        summary.crashed = True
        return summary

    block = format_summary_block(summary)
    if summary.failures:
        logger.error(block)
    else:
        logger.info(block)

    if persist:
        summary.persisted = _persist_results(summary)

    if send_alert_email and summary.failures:
        if alert_emails_enabled():
            summary.email_sent = _send_consolidated_alert(summary)
        else:
            logger.info(
                "Model-backend health alert email suppressed "
                "(test/dev environment or FHI_MODEL_HEALTH_ALERT_EMAIL=0)"
            )
    return summary

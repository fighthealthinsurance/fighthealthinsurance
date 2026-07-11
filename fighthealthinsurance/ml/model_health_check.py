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
  client-init failure, auth failure, unknown model name, rate limiting/quota,
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

from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml.ml_models import (
    ModelDescription,
    RateLimitedRemoteOpenLike,
    RemoteModel,
    RemoteModelLike,
    candidate_model_backends,
)
from fighthealthinsurance.ml.ml_router import MLRouter

# Where the consolidated failure alert is sent. Matches the on-call alias used
# by the existing model-liveness alerts.
SUPPORT_EMAIL = "support42@fighthealthinsurance.com"

# The tiny prompt used for the end-to-end check. Kept deliberately short so
# each probe costs a handful of tokens.
HEALTH_CHECK_PROMPT = "Reply with exactly: OK"
HEALTH_CHECK_SYSTEM_PROMPT = (
    "You are part of an automated health check. Reply with exactly: OK"
)

# --- Result categories ------------------------------------------------------
CATEGORY_PASS = "PASS"
# The backend answered, but the router never registered it — it would be
# invisible to the selection UI and usage reporting despite working.
CATEGORY_PASS_UNREGISTERED = "PASS_UNREGISTERED"
# Provider intentionally off (none of its configuration present).
CATEGORY_NOT_CONFIGURED = "NOT_CONFIGURED"
# Excluded by the ENABLED_REMOTE_MODELS allow-list; never invoked.
CATEGORY_DISABLED = "DISABLED"
CATEGORY_MISSING_CREDENTIALS = "FAIL_MISSING_CREDENTIALS"
CATEGORY_CLIENT_INIT = "FAIL_CLIENT_INIT"
CATEGORY_AUTH = "FAIL_AUTH"
CATEGORY_MODEL_NOT_FOUND = "FAIL_MODEL_NOT_FOUND"
CATEGORY_RATE_LIMITED = "FAIL_RATE_LIMITED"
CATEGORY_TIMEOUT = "FAIL_TIMEOUT"
CATEGORY_NETWORK = "FAIL_NETWORK"
CATEGORY_MALFORMED_RESPONSE = "FAIL_MALFORMED_RESPONSE"
CATEGORY_OTHER = "FAIL_OTHER"

# Categories that count as failures for alerting/strict mode. PASS variants
# and the two intentionally-off categories are not failures — but
# PASS_UNREGISTERED is called out separately in the summary and email because
# it means users can't see a model that works.
FAILURE_CATEGORIES = frozenset(
    {
        CATEGORY_MISSING_CREDENTIALS,
        CATEGORY_CLIENT_INIT,
        CATEGORY_AUTH,
        CATEGORY_MODEL_NOT_FOUND,
        CATEGORY_RATE_LIMITED,
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
    """Values of secret-looking environment variables, longest first so
    replacement of a long value can't be defeated by a shorter one matching a
    substring earlier."""
    values = [
        v
        for k, v in os.environ.items()
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
    started_at: Optional[datetime] = None

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
    email_sent: bool = False
    persisted: bool = False

    @property
    def failures(self) -> List[BackendCheckResult]:
        return [r for r in self.results if r.failed]

    @property
    def unregistered_passes(self) -> List[BackendCheckResult]:
        return [r for r in self.results if r.category == CATEGORY_PASS_UNREGISTERED]


def deployment_id() -> str:
    """Deployment/version identifier for this process.

    Checks ``FHI_DEPLOYMENT_ID``, then ``FHI_RELEASE`` (baked into the image
    from the build arg), then ``FHI_VERSION``. Falls back to a coarse UTC
    hour stamp so unversioned environments still get a usable leader-claim
    key (deduping reruns within the hour).
    """
    for var in ("FHI_DEPLOYMENT_ID", "FHI_RELEASE", "FHI_VERSION"):
        value = os.getenv(var)
        if value and value.strip():
            return value.strip()
    return "unversioned-" + datetime.now(dt_timezone.utc).strftime("%Y%m%d%H")


def environment_name() -> str:
    return os.getenv("DJANGO_CONFIGURATION") or os.getenv("ENVIRONMENT") or "unknown"


def strict_mode_enabled() -> bool:
    """Whether a failed backend should fail the deployment (default: no)."""
    return os.getenv("FHI_MODEL_HEALTH_STRICT", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def alert_emails_enabled() -> bool:
    """Whether the consolidated failure alert email may be sent.

    ``FHI_MODEL_HEALTH_ALERT_EMAIL=1`` forces on (even in dev/test),
    ``FHI_MODEL_HEALTH_ALERT_EMAIL=0`` forces off. Otherwise alerts are
    disabled in test runs (``TESTING=True``) and DEBUG (local dev)
    environments, and enabled elsewhere (production).
    """
    override = os.getenv("FHI_MODEL_HEALTH_ALERT_EMAIL", "").strip()
    if override == "1":
        return True
    if override == "0":
        return False
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


def _registry_flags(
    desc: ModelDescription, instance: Optional[RemoteModelLike]
) -> Tuple[bool, bool]:
    """(ui_registered, reporting_registered) for a model description.

    * ``ui_registered``: the registered instance is in one of the pools the
      router offers for generation/context work — i.e. it can actually be
      selected and can therefore show up in the chooser / selection UI.
    * ``reporting_registered``: the friendly name is present in
      ``models_by_name`` — the name-stamping registry that usage reporting
      (ProposedAppeal.model_name / ChooserCandidate.model_name) records.
    """
    try:
        router = _router()
        reporting = bool(router.models_by_name.get(desc.name))
        ui = False
        if instance is not None:
            pool_ids = {
                id(m)
                for m in list(router.all_models_by_cost)
                + list(router.context_only_models_by_cost)
            }
            ui = id(instance) in pool_ids
        return ui, reporting
    except Exception:
        logger.opt(exception=True).warning("Could not compute registry flags")
        return False, False


def enumerate_backend_checks(
    only_models: Optional[List[str]] = None,
) -> Tuple[List[BackendCheckResult], List[Tuple[BackendCheckResult, RemoteModelLike]]]:
    """Walk every candidate backend class and classify its configuration.

    Returns ``(static_results, checkable)``:

    * ``static_results`` — rows that are decided without any network call:
      not-configured providers, allow-list-disabled models, missing
      credentials, and client-construction failures.
    * ``checkable`` — ``(pending_result, instance)`` pairs for enabled,
      constructable backends that should actually be invoked.

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

    for backend_cls in candidate_model_backends:
        try:
            catalog = backend_cls.model_catalog()
        except Exception as e:
            logger.opt(exception=True).warning(
                f"model_catalog() failed for {backend_cls.__name__}: {e}"
            )
            catalog = []
        if not catalog:
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
            if not _wanted(desc):
                continue
            base = BackendCheckResult(
                provider=provider,
                model_name=desc.name,
                internal_name=desc.internal_name,
                category=CATEGORY_OTHER,
            )

            if status == "not_configured":
                base.category = CATEGORY_NOT_CONFIGURED
                base.enabled = False
                base.error = sanitize_error(detail)
                static_results.append(base)
                continue
            if status == "missing_credentials":
                base.category = CATEGORY_MISSING_CREDENTIALS
                base.error = sanitize_error(detail)
                static_results.append(base)
                continue

            instance = _registered_instance(backend_cls, desc)
            base.ui_registered, base.reporting_registered = _registry_flags(
                desc, instance
            )

            # The ENABLED_REMOTE_MODELS allow-list only gates remote
            # generation models (mirrors MLRouter registration).
            probe_instance: Optional[RemoteModelLike] = instance
            if probe_instance is None:
                try:
                    probe_instance = backend_cls(model=desc.internal_name)
                except EnvironmentError as e:
                    base.category = CATEGORY_MISSING_CREDENTIALS
                    base.error = sanitize_error(str(e))
                    static_results.append(base)
                    continue
                except Exception as e:
                    base.category = CATEGORY_CLIENT_INIT
                    base.error = sanitize_error(f"{type(e).__name__}: {e}")
                    static_results.append(base)
                    continue

            if (
                enabled_names is not None
                and probe_instance.external
                and not probe_instance.context_only
                and desc.name not in enabled_names
                and desc.internal_name not in enabled_names
            ):
                base.category = CATEGORY_DISABLED
                base.enabled = False
                base.error = "excluded by ENABLED_REMOTE_MODELS"
                static_results.append(base)
                continue

            checkable.append((base, probe_instance))

    return static_results, checkable


# --- Invocation --------------------------------------------------------------


def _categorize_http_error(e: aiohttp.ClientResponseError) -> Tuple[str, str]:
    detail = sanitize_error(f"HTTP {e.status} {e.message or ''}")
    if e.status in (401, 403):
        return CATEGORY_AUTH, detail
    if e.status == 404:
        return CATEGORY_MODEL_NOT_FOUND, detail
    if e.status in (429, 402):
        return CATEGORY_RATE_LIMITED, detail
    if e.status == 400 and "model" in (e.message or "").lower():
        # Providers that reject unknown model ids with a 400 mentioning the
        # model field (rather than a clean 404).
        return CATEGORY_MODEL_NOT_FOUND, detail
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

    start = time.monotonic()
    try:
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
        result.category = CATEGORY_MALFORMED_RESPONSE
        result.error = "empty or no text in provider response"
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
    lines = []
    for r in failures:
        lines.append(
            f"- provider: {r.provider}\n"
            f"  model: {r.model_name} (internal: {r.internal_name or 'n/a'})\n"
            f"  category: {r.category}\n"
            f"  error: {r.error or 'n/a'}\n"
            f"  registered for selection UI: {'yes' if r.ui_registered else 'NO'}; "
            f"recognized by reporting: {'yes' if r.reporting_registered else 'NO'}"
        )
    for r in summary.unregistered_passes:
        lines.append(
            f"- provider: {r.provider}\n"
            f"  model: {r.model_name} (internal: {r.internal_name or 'n/a'})\n"
            f"  category: {r.category} (model WORKS but is not registered — "
            f"it will not appear in the selection UI / reporting)\n"
            f"  detail: {r.error or 'n/a'}"
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
        f"Failing backends:\n\n" + "\n\n".join(lines) + "\n\n"
        "Each backend was tested once with a tiny 'Reply with exactly: OK' "
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


def try_claim_deployment_leader(deploy_id: str) -> bool:
    """Claim the once-per-deployment leader slot via the shared database.

    Exactly one caller across every pod/process sharing the database wins for
    a given deployment id (within ``LEADER_CLAIM_WINDOW_SECONDS``). On any
    database error we return ``False`` — better to occasionally skip the check
    than to have every worker run it and email support in parallel.
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
        return False


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
      returned summary has ``ran_checks=False`` and nothing is invoked.
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

    if require_leader and not try_claim_deployment_leader(deploy_id):
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

"""Address and directly query one specific ML model backend.

The staff system-status page lists every registered backend and whether it
answers a liveness check. When a row looks wrong the next question is always
"what does it actually say?", so this module lets staff send an arbitrary
prompt straight at one backend and capture the raw response -- bypassing the
router's selection and fan-out so the answer is attributable to that backend
alone.

Backends are addressed by a **reference string** rather than a Python object
id (as ``health_status._model_key`` uses): several web pods each build their
own ``MLRouter``, so a link generated while rendering the status page on one
pod has to resolve on whichever pod serves the follow-up request. The
reference is therefore built from stable configuration -- implementation
class, router-stamped friendly name, configured wire model id -- plus an
occurrence index that keeps genuinely identical replicas apart. The router's
pools are cost-sorted from the same configuration in every process, so that
index means the same thing everywhere.

Queries reuse ``_infer_no_context`` (the same entry point as the startup
``probe``) with ``raise_http_errors=True`` so auth/quota/model-not-found
failures surface as a real HTTP status instead of being flattened into
"empty response".
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asgiref.sync import async_to_sync
from loguru import logger

from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml.ml_models import RemoteModelLike, describe_model_error

# Field separator inside a reference string. Neither a class name, a friendly
# model name nor a wire model id contains it, so no escaping is needed.
_REF_SEP = "|"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the question directly and concisely."
)
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT = 60.0
# Bounds for staff-supplied values; a direct query is a debugging tool, not a
# way to wait on a backend indefinitely.
#
# MAX_TIMEOUT matches ``RemoteOpenLike._timeout`` (300s), the backend's own
# per-inference ceiling: past that the model layer gives up on its own, so a
# larger value here would buy nothing. It sits well inside the ingress
# ``proxy-read-timeout`` (3600s in k8s/deploy*.yaml), so a query that runs to
# the ceiling still returns a real answer rather than a gateway error. Only a
# staff user who deliberately raises the field gets near it -- the default is
# a minute -- and the sync view runs in the ASGI thread pool, so a long query
# holds one thread rather than blocking its uvicorn worker.
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 300.0
MAX_PROMPT_LENGTH = 20000


def _base_ref(model: Any) -> str:
    """Configuration-derived (not yet unique) reference for ``model``."""
    name = getattr(model, "name", None) or ""
    wire = getattr(model, "model", None) or ""
    return _REF_SEP.join([type(model).__name__, str(name), str(wire)])


def build_refs(models: Sequence[Any]) -> List[str]:
    """Reference string for each model in ``models``, positionally aligned.

    Identical configurations (e.g. internal replicas of one model on several
    hosts) share a base reference, so each gets the index of its occurrence
    appended. Because the caller always passes the pools in their stable
    cost-sorted order, the same backend gets the same reference in every
    process.
    """
    seen: Dict[str, int] = {}
    refs: List[str] = []
    for model in models:
        base = _base_ref(model)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        refs.append(f"{base}{_REF_SEP}{occurrence}")
    return refs


def registered_models() -> List[RemoteModelLike]:
    """Every backend a staff query can target, deduplicated by identity.

    Context-only backends (e.g. Perplexity for citations) are included and
    appended last: they never take part in generation, but they are exactly
    the ones whose quota/credential failures are worth poking at by hand.
    Appending them keeps the occurrence indices of the generation pools
    unchanged, so a reference stays valid whether or not the context-only
    pool is reachable.
    """
    router = ml_router_module.ml_router
    models: List[RemoteModelLike] = list(router.all_models_by_cost)
    try:
        context_only = list(router.context_only_models_by_cost)
    except Exception:
        # A router that cannot enumerate its context-only pool should still
        # let staff query the generation backends.
        logger.opt(exception=True).debug(
            "Could not enumerate context-only models for direct query"
        )
        context_only = []
    seen = {id(m) for m in models}
    for m in context_only:
        if id(m) not in seen:
            seen.add(id(m))
            models.append(m)
    return models


def enumerate_queryable_models() -> List[Tuple[str, RemoteModelLike]]:
    """``(reference, model)`` for every backend, in stable pool order."""
    models = registered_models()
    return list(zip(build_refs(models), models))


def model_refs_by_identity() -> Dict[int, str]:
    """``id(model) -> reference`` for the current process's backends.

    Lets a caller that already holds model instances (the health sweep) label
    them without re-deriving the ordering rules.
    """
    return {id(model): ref for ref, model in enumerate_queryable_models()}


def find_model_by_ref(ref: Optional[str]) -> Optional[RemoteModelLike]:
    """Resolve a reference string to a live backend, or ``None``."""
    if not ref:
        return None
    for candidate_ref, model in enumerate_queryable_models():
        if candidate_ref == ref:
            return model
    return None


def describe_model(model: Any, ref: Optional[str] = None) -> Dict[str, Any]:
    """Template-friendly summary of a backend.

    ``dual_mode``/``backup_model`` are surfaced because they change how a
    response should be read: a dual-mode backend races a backup endpoint, so
    the text may have come from the backup rather than the primary.
    """
    backup_model = getattr(model, "backup_model", None)
    dual_mode = bool(getattr(model, "dual_mode", False)) and bool(
        getattr(model, "backup_api_base", None)
    )
    return {
        "ref": ref if ref is not None else _base_ref(model) + f"{_REF_SEP}0",
        "label": str(model),
        "class_name": type(model).__name__,
        "name": getattr(model, "name", None),
        "wire_model": getattr(model, "model", None),
        "api_base": getattr(model, "api_base", None),
        "external": bool(getattr(model, "external", True)),
        "context_only": bool(getattr(model, "context_only", False)),
        "dual_mode": dual_mode,
        "backup_model": backup_model if dual_mode else None,
        "backup_api_base": (
            getattr(model, "backup_api_base", None) if dual_mode else None
        ),
    }


def clamp_timeout(raw: Any, default: float = DEFAULT_TIMEOUT) -> float:
    """Parse a staff-supplied timeout, falling back to the default."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def clamp_temperature(raw: Any, default: float = DEFAULT_TEMPERATURE) -> float:
    """Parse a staff-supplied temperature, falling back to the default."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(2.0, value))


async def aquery_model(
    model: Any,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Send ``prompt`` to ``model`` and capture what comes back.

    Returns ``{"ok", "response", "error", "elapsed"}``. Never raises: a
    failure is reported as ``ok=False`` with a one-line reason, since the
    reason is the point of running this from a status page.
    """
    # ``_infer`` loops over the system prompts, so an empty list would return
    # None without ever calling the backend -- always send at least one.
    system_prompts = [(system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT]
    start = time.monotonic()
    try:
        response = await asyncio.wait_for(
            model._infer_no_context(
                system_prompts=system_prompts,
                prompt=prompt,
                temperature=temperature,
                raise_http_errors=True,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "response": None,
            "error": f"timeout>{timeout:g}s",
            "elapsed": time.monotonic() - start,
        }
    except Exception as e:
        logger.warning(
            f"Staff direct query to {model} failed: {describe_model_error(e)}"
        )
        return {
            "ok": False,
            "response": None,
            "error": describe_model_error(e),
            "elapsed": time.monotonic() - start,
        }
    elapsed = time.monotonic() - start
    if response is None or not response.strip():
        return {
            "ok": False,
            "response": response,
            "error": "empty or no response",
            "elapsed": elapsed,
        }
    return {"ok": True, "response": response, "error": None, "elapsed": elapsed}


def query_model(
    model: Any,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Synchronous wrapper around :func:`aquery_model` for the staff view."""
    return async_to_sync(aquery_model)(
        model,
        prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        timeout=timeout,
    )

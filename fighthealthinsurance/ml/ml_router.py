import asyncio
import random
import threading
import time
from typing import List, Optional, Sequence, Tuple

from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable
from fighthealthinsurance.ml.chat_policy import ChatPolicy, narrow_externals

# How many outside models chat asks at most, and the draw exploration uses
# (a seam, so tests can decide it).
CHAT_OUTSIDE_LIMIT = 3
_explore_draw = random.random
from fighthealthinsurance.ml.ml_models import *
from fighthealthinsurance.ml.retired_models import retirement

# The hosted model that backs up our own models for summaries and appeal
# questions (DeepInfra's Gemma). Named once so the two paths can't drift apart.
_EXTERNAL_GENERALIST = "google/gemma-4-26B-A4B-it"

# When the "every outside chat model is down" warning last went out: once an
# hour (the health sweep's cadence) says it, a line per chat turn would not.
_ROSTER_DOWN_WARN_SECONDS = 3600.0
_roster_down_warned_at = float("-inf")


def _warn_roster_down(names: list[str]) -> None:
    global _roster_down_warned_at
    now = time.monotonic()
    if now - _roster_down_warned_at < _ROSTER_DOWN_WARN_SECONDS:
        return
    _roster_down_warned_at = now
    logger.warning(
        f"MLRouter: every outside chat model ({names}) is down or out of "
        f"budget; chat is answered by our own models alone"
    )


class MLRouter(object):
    """
    Tool to route our requests most cheapily.
    """

    # Type hints only (not mutable class-level defaults)
    models_by_name: dict[str, List[RemoteModelLike]]
    internal_models_by_cost: List[RemoteModelLike]
    all_models_by_cost: List[RemoteModelLike]
    external_models_by_cost: List[RemoteModelLike]
    context_only_models_by_cost: List[RemoteModelLike]
    chat_outside_models_by_name: dict[str, RemoteModelLike]

    def __init__(self):
        # Initialize instance attributes to avoid mutable class-level state
        self.models_by_name = {}
        self.internal_models_by_cost = []
        self.all_models_by_cost = []
        self.external_models_by_cost = []
        self.context_only_models_by_cost = []
        self.chat_outside_models_by_name = {}
        logger.debug("MLRouter: starting model registration")
        enabled_models = self._enabled_model_names()
        if enabled_models is not None:
            logger.info(
                f"MLRouter: ENABLED_REMOTE_MODELS set; only enabling "
                f"{sorted(enabled_models)}"
            )
        building_internal_models_by_cost = []
        building_external_models_by_cost = []
        building_all_models_by_cost = []
        building_context_only_models_by_cost = []
        building_models_by_name: dict[str, List[ModelDescription]] = {}
        # Sorted by class name: all_subclasses() yields a set, so without
        # this the registration order -- and everything downstream that
        # depends on it (models_by_name insertion order, which fhi-* backend
        # chat doubles, dual-mode pairings) -- varied per process. Every pod
        # must route identically.
        for backend in sorted(candidate_model_backends, key=lambda c: c.__name__):
            # Per-backend guard: a backend whose models() blows up is skipped
            # without affecting the others.
            try:
                models = backend.models()
            except Exception as e:
                logger.warning(f"Skipping {backend} due to {e} of {type(e)}")
                continue
            logger.debug(f"MLRouter: {backend} provided {len(models)} models")
            for m in models:
                # Per-model guard: one bad model description (constructor
                # failure, bad config) must skip only ITSELF -- previously the
                # whole backend's remaining models were dropped with it.
                try:
                    retired = retirement(m.name, m.internal_name)
                    if retired is not None:
                        logger.info(
                            f"MLRouter: skipping retired model {m.name} "
                            f"({retired.describe()})"
                        )
                        continue
                    if m.model is None:
                        m.model = backend(model=m.internal_name)
                    # Honor the ENABLED_REMOTE_MODELS allow-list (if set): only
                    # register a *remote* generation model whose friendly name
                    # (or internal name) is listed. Always-enabled regardless of
                    # the allow-list: local/internal models, and context-only
                    # models (e.g. Perplexity citations) which are a separate
                    # special-purpose pool. When the variable is unset, every
                    # model is enabled.
                    if (
                        enabled_models is not None
                        and m.model.external
                        and not m.model.context_only
                        and m.name not in enabled_models
                        and m.internal_name not in enabled_models
                    ):
                        logger.debug(
                            f"MLRouter: skipping disabled remote model {m.name} "
                            f"(not in ENABLED_REMOTE_MODELS)"
                        )
                        continue
                    # Stamp the friendly tracking name onto the instance so
                    # consumers that hold a model *instance* (e.g. the chooser
                    # in chooser_tasks.py) record this stable, human-readable
                    # name instead of a raw object repr. The name also matches
                    # the models_by_name keys used by the regular workflow.
                    if getattr(m.model, "name", None) is None:
                        try:
                            m.model.name = m.name
                        except Exception as e:
                            logger.debug(
                                f"MLRouter: could not stamp name on "
                                f"{m.internal_name}: {e}"
                            )
                    # Context-only models (e.g. Perplexity) are reserved for
                    # building context such as citations and must never appear
                    # in the general generation pools. They remain reachable by
                    # name via models_by_name for the context-building methods.
                    if m.model.context_only:
                        building_context_only_models_by_cost.append(m)
                    else:
                        if not m.model.external:
                            building_internal_models_by_cost.append(m)
                        else:
                            building_external_models_by_cost.append(m)
                        building_all_models_by_cost.append(m)
                    same_models: list[ModelDescription] = []
                    if m.name in building_models_by_name:
                        same_models = building_models_by_name[m.name]
                    same_models.append(m)
                    building_models_by_name[m.name] = same_models
                except Exception as e:
                    logger.warning(
                        f"Skipping model {getattr(m, 'internal_name', m)} of "
                        f"{backend} due to {e} of {type(e)}"
                    )
        for k, v in building_models_by_name.items():
            sorted_model_descriptions: list[ModelDescription] = sorted(v)
            self.models_by_name[k] = [
                x.model for x in sorted_model_descriptions if x.model is not None
            ]
        self.internal_models_by_cost = [
            x.model
            for x in sorted(building_internal_models_by_cost)
            if x.model is not None
        ]
        self.all_models_by_cost = [
            x.model for x in sorted(building_all_models_by_cost) if x.model is not None
        ]
        self.external_models_by_cost = [
            x.model
            for x in sorted(building_external_models_by_cost)
            if x.model is not None
        ]
        self.context_only_models_by_cost = [
            x.model
            for x in sorted(building_context_only_models_by_cost)
            if x.model is not None
        ]
        self._register_chat_outside_models()
        logger.info(
            f"MLRouter initialized with {len(self.all_models_by_cost)} total models, "
            f"{len(self.internal_models_by_cost)} internal, {len(self.external_models_by_cost)} external, "
            f"{len(self.context_only_models_by_cost)} context-only"
        )
        logger.info(f"All loaded models: {[str(m) for m in self.all_models_by_cost]}")
        logger.debug(
            f"Built {self} with i:{self.internal_models_by_cost} a:{self.all_models_by_cost}"
        )

    def _register_chat_outside_models(self) -> None:
        """Instances of the models backends serve to chat only
        (``chat_models``), by name, outside every general pool. A backend
        without its key, or a model that fails to build, is skipped."""
        # The same allow-list as every other remote model: a provider the
        # operator left out never gets chat text.
        enabled_models = self._enabled_model_names()
        for backend in sorted(candidate_model_backends, key=lambda c: c.__name__):
            try:
                descriptions = backend.chat_models()
            except Exception as e:
                logger.warning(f"Skipping chat models of {backend}: {type(e).__name__}")
                continue
            for m in descriptions:
                if retirement(m.name, m.internal_name) is not None:
                    logger.info(f"MLRouter: skipping retired chat model {m.name}")
                    continue
                if (
                    enabled_models is not None
                    and m.name not in enabled_models
                    and m.internal_name not in enabled_models
                ):
                    logger.debug(
                        f"MLRouter: skipping disabled chat model {m.name} "
                        f"(not in ENABLED_REMOTE_MODELS)"
                    )
                    continue
                try:
                    if m.model is None:
                        m.model = backend(model=m.internal_name)
                    if getattr(m.model, "name", None) is None:
                        m.model.name = m.name
                    self.chat_outside_models_by_name[m.name] = m.model
                except Exception as e:
                    logger.warning(
                        f"Skipping chat model {m.internal_name}: {type(e).__name__}"
                    )

    def chat_outside_models(
        self,
        names: Optional[Sequence[str]] = None,
        limit: int = 3,
        warn_if_down: bool = True,
    ) -> list[RemoteModelLike]:
        """The outside models chat asks, in ``names`` order (default
        FHI_CHAT_OUTSIDE_MODELS): the chat-only models, or any registered
        external model by name (Azure's GPT-5.5). Models that are down are
        left out, so the next ones on the roster take their places; and so
        is any model whose provider's chat budget is spent (a spent budget
        means no call). Only when every roster model is down and none of
        ours can answer either does this fail open like the other filters,
        so a wrong health signal cannot leave a turn with no one to ask."""
        from django.conf import settings

        from fighthealthinsurance.ml import spend

        if names is None:
            names = getattr(settings, "FHI_CHAT_OUTSIDE_MODELS", None) or []
        found: list[RemoteModelLike] = []
        for name in names:
            model = self.chat_outside_models_by_name.get(name)
            if model is None:
                model = next(
                    (m for m in self.models_by_name.get(name, []) if m.external),
                    None,
                )
            if model is not None and model not in found:
                found.append(model)
        available = [m for m in found if self._selectable(m)]
        if found and not available:
            if self.chat_internal_selectable():
                # Expected once outside models are retired or out of credit:
                # ours answer, and the roster is optional.
                if warn_if_down:
                    _warn_roster_down([str(m) for m in found])
                return []
            available = self._filter_available(found, "chat-outside")
        within_budget = [
            m
            for m in available
            if getattr(m, "SPEND_PROVIDER", None) is None
            or spend.allows(getattr(m, "SPEND_PROVIDER"), spend.CHAT)
        ]
        return within_budget[:limit]

    def chat_side_by_side_model(self) -> Optional[RemoteModelLike]:
        """The model a crucial chat turn compares with its reply
        (FHI_CHAT_SIDE_BY_SIDE_MODEL, Kimi-K3 by default), or None when it
        is unset, not registered, down or over its provider's chat budget
        (the same filters as chat_outside_models)."""
        from django.conf import settings

        name = str(getattr(settings, "FHI_CHAT_SIDE_BY_SIDE_MODEL", "") or "").strip()
        if not name:
            return None
        # One model, not the roster: its being down says nothing about the
        # others, so it neither warns nor spends the hourly warning.
        found = self.chat_outside_models([name], limit=1, warn_if_down=False)
        return found[0] if found else None

    @staticmethod
    def _enabled_model_names() -> Optional[set[str]]:
        """Parse the ``ENABLED_REMOTE_MODELS`` allow-list.

        Returns a set of enabled *remote* model names (friendly names as shown
        in the "All loaded models" log, and/or internal names) when the
        environment variable is set and non-empty, or ``None`` to mean "no
        restriction" (the default when the variable is absent/blank). Entries
        are comma-separated; surrounding whitespace is ignored.

        The allow-list only gates remote (external) generation models.
        Local/internal models and context-only models (e.g. Perplexity
        citations) are always enabled regardless of this setting.
        """
        raw = get_env_variable("ENABLED_REMOTE_MODELS")
        if not raw or not raw.strip():
            return None
        names = {n.strip() for n in raw.split(",") if n.strip()}
        return names or None

    def best_external_models(self, limit: int = 3) -> list[RemoteModelLike]:
        """Return up to ``limit`` external models, best-quality first, limited to
        those currently available.

        "Best" means highest ``quality()`` (descending); because
        ``external_models_by_cost`` is cost-ordered and the sort is stable,
        cheaper models win ties. Availability is judged from cheap, non-blocking
        signals only — never a live network probe on the request path (see
        :meth:`_external_selectable`): the model's in-memory ``is_available()``
        plus, for backends without a live signal, the last cached
        ``health_status`` sweep.

        Replaces the previous "cheapest N external" slices so that, instead of
        fanning out across every external backend, we route to the strongest
        few that are up.
        """
        available = [m for m in self.external_models_by_cost if self._selectable(m)]
        best = sorted(available, key=lambda m: -m.quality())
        return best[:limit]

    def _selectable(self, model: RemoteModelLike) -> bool:
        """Whether a model should be offered for fan-out, using only cheap,
        non-blocking signals — no live network calls in the router.

        * ``is_available()`` — the model's own in-memory signal (paid providers
          report config + rate-limit back-off; others fail open).
        * the last cached ``health_status`` sweep — consulted only for models
          that lack a live signal (``health_checked_live`` is False, e.g.
          DeepInfra and the INTERNAL vLLM backends, which the sweep probes over
          the network). Models the last sweep marked unhealthy are skipped;
          ones it hasn't checked yet fail open, so we assume a backend is
          available until a real check says otherwise.

        Applies to internal backends as much as external ones: an internal
        vLLM the sweep already marked down used to be fanned out to anyway,
        burning its whole timeout on every request between sweeps.
        """
        if not model.is_available():
            return False
        # A provider whose budget for this use is spent, or that refused for
        # credit or quota today (ml/spend.py), would answer nothing: its slot
        # goes to a model that can. The send checks it again (__infer), for a
        # model chosen before the budget ran out.
        spend_allows = getattr(model, "_spend_allows", None)
        if spend_allows is not None and not spend_allows():
            return False
        if not model.health_checked_live:
            # Imported lazily to avoid a circular import (health_status imports
            # the router). model_ok() is a lock-free cache read.
            from fighthealthinsurance.ml.health_status import health_status

            if health_status.model_ok(model) is False:
                return False
        return True

    # Backwards-compatible alias (tests and older call sites patch/call this
    # under the external-specific name).
    def _external_selectable(self, model: RemoteModelLike) -> bool:
        return self._selectable(model)

    def _filter_available(
        self, models: Sequence[RemoteModelLike], context: str
    ) -> list[RemoteModelLike]:
        """Filter ``models`` down to the currently-selectable ones.

        FAIL OPEN when the filter would empty a non-empty list: a stale or
        wrong health cache must never zero out generation entirely -- in that
        case every candidate is returned (with an ERROR logged) and the
        per-call timeouts bound the damage.
        """
        candidates = list(models)
        if not candidates:
            return candidates
        available = [m for m in candidates if self._selectable(m)]
        if not available:
            logger.error(
                f"MLRouter: every candidate for {context} "
                f"({[str(m) for m in candidates]}) is marked unavailable; "
                f"failing open with the full list"
            )
            return candidates
        return available

    def _general_purpose_only(
        self, models: Sequence[RemoteModelLike], context: str
    ) -> list[RemoteModelLike]:
        """Drop backends that only produce their fine-tuned artifact.

        Chat turns, tool calls, entity extraction, QA and summarization all
        need a model that follows instructions; the appeal-text fine-tunes
        (fhi-legacy) answer those with blank lines and stray digits, which
        cost a fan-out slot and a full timeout every time. Generation paths
        (appeals, prior auth) deliberately do NOT call this.

        Fails open like ``_filter_available``: if every candidate is narrow,
        keep them rather than returning nothing to route to.
        """
        candidates = list(models)
        general = [m for m in candidates if m.supports_general_instructions()]
        if not general:
            if candidates:
                # WARNING, not DEBUG: falling back means an instruction-following
                # path is about to be served by an appeal-text fine-tune, which
                # is the failure this filter exists to prevent. It is still
                # better than routing nowhere, but it should be visible.
                logger.warning(
                    f"MLRouter: no general-purpose backend for {context}; "
                    f"falling back to {[str(m) for m in candidates]}"
                )
            return candidates
        return general

    def _healthy_general_internal(self) -> list[RemoteModelLike]:
        """Internal backends that follow instructions and look healthy,
        strongest first.

        Strict, unlike ``_filter_available`` and ``_general_purpose_only``:
        it never fails open, so it can be empty. Callers use it to decide
        whether one of our own models can take a task before a hosted one is
        asked, and a marked-down or appeal-only backend must not count as
        "ours can do it". The sort is stable over the cost order, so
        equal-quality backends stay cheapest-first.
        """
        return sorted(
            (
                m
                for m in self.internal_models_by_cost
                if m.supports_general_instructions() and self._selectable(m)
            ),
            key=lambda m: -m.quality(),
        )

    def _external_generalist(self) -> list[RemoteModelLike]:
        """The hosted generalist's instances that look healthy, cheapest first.

        Empty when DeepInfra isn't configured or the health signals have it
        down. Callers decide whether ``use_external`` allows it at all.
        """
        return [
            m
            for m in self.models_by_name.get(_EXTERNAL_GENERALIST, [])
            if self._selectable(m)
        ]

    def _get_forced_models(
        self, task_description: str = "", *, use_external: bool = True
    ) -> Optional[list[RemoteModelLike]]:
        """Check for FORCE_MODEL environment variable and return forced models if set.

        Args:
            task_description: Optional description for logging (e.g., "for text generation")

        Returns:
            List of forced models if FORCE_MODEL is set and models are found, None otherwise
        """
        forced_model = get_env_variable("FORCE_MODEL")
        if not forced_model:
            return None

        logger.info(
            f"FORCE_MODEL={forced_model} {task_description}; "
            f"available: {[type(m).__name__ for m in self.all_models_by_cost]}"
        )

        if forced_model in self.models_by_name:
            # Force a specific model by name
            forced_models = self.models_by_name[forced_model]
            if forced_models:
                # Filter by use_external flag
                if not use_external:
                    filtered_models = [m for m in forced_models if not m.external]
                    if filtered_models:
                        logger.info(
                            f"✓ Forcing model {forced_model} {task_description} (filtered to internal only): {[getattr(m, 'model', type(m).__name__) for m in filtered_models]}"
                        )
                        return filtered_models
                    else:
                        logger.warning(
                            f"FORCE_MODEL={forced_model} ignored {task_description} because use_external=False and all instances are external"
                        )
                        return None
                else:
                    logger.info(
                        f"✓ Forcing model {forced_model} {task_description}: {[getattr(m, 'model', type(m).__name__) for m in forced_models]}"
                    )
                    return forced_models

        logger.warning(
            f"Forced model {forced_model} not found, falling back to default"
        )
        return None

    def entity_extract_backends(self, use_external) -> list[RemoteModelLike]:
        """Backends for entity extraction."""
        if use_external:
            return self._general_purpose_only(
                self._filter_available(self.all_models_by_cost, "entity-extract"),
                "entity-extract",
            )
        else:
            return self._general_purpose_only(
                self._filter_available(
                    self.internal_models_by_cost, "entity-extract-internal"
                ),
                "entity-extract-internal",
            )

    def generate_text_backends(
        self, use_external: bool = False
    ) -> list[RemoteModelLike]:
        """Return models for text generation tasks like prior authorization and ongoing chat."""
        # Check for forced model override
        forced_models = self._get_forced_models(
            "for text generation", use_external=use_external
        )
        if forced_models:
            return forced_models

        # NOTE: there used to be an early return here preferring DeepInfra's
        # Llama-4-Scout for all text generation. It went dead when the model
        # was dropped from the DeepInfra catalog, and it is deliberately NOT
        # re-pointed at a successor: the early return bypassed the
        # ``use_external`` privacy gate below, routing internal-only requests
        # to an external model whenever DeepInfra was configured.

        # Internal models first, optionally appending external if allowed.
        # Filter BEFORE slicing so healthy backends past the slice boundary
        # can step up when the cheapest ones are marked down.
        models: list[RemoteModelLike] = []
        if self.internal_models_by_cost:
            models += self._filter_available(
                self.internal_models_by_cost, "generate-text-internal"
            )[:6]
        if use_external:
            models += self.best_external_models()
        # Only fall back to all_models if use_external is True
        if not models and use_external:
            # Keep the availability gate authoritative: don't re-introduce
            # externals that best_external_models() just filtered out. Internal
            # backends are always eligible; externals must pass the same gate.
            models = [
                m
                for m in self.all_models_by_cost
                if not m.external or self._selectable(m)
            ][:6]
        return models

    def generate_text_backend_names(self, use_external: bool = False) -> list[str]:
        """
        Return model NAMES for text generation, preserving multi-backend support.

        WHY THIS APPROACH:
        Returns model names (strings) instead of model instances to preserve the ability
        to call multiple backend servers running the same model. When a model name is
        returned, the caller looks it up in models_by_name to get ALL instances (e.g.,
        3 different servers running "fhi-2025") and tries them sequentially as fallbacks.

        If we returned model instances directly, we'd lose multi-backend redundancy since
        each backend class only creates ONE instance per model name, even if multiple
        servers are available.

        FLOW:
        1. Router returns ["fhi-2025", "deepseek-ai/DeepSeek-V4-Pro"]
        2. Caller looks up models_by_name["fhi-2025"] → [server1, server2, server3]
        3. Caller submits to the first server that accepts the submission and
           moves to the next only when a submission fails (get_model_result);
           an inference-time failure is that server's failure.
        Context-only backends (Perplexity "sonar") are never returned: they
        build citations and do not draft.

        Args:
            use_external: Whether to include external models

        Returns:
            List of model names (not instances) to use for text generation
        """
        # Check for forced model override
        forced_model = get_env_variable("FORCE_MODEL")
        if forced_model:
            logger.info(f"FORCE_MODEL={forced_model} for text generation")

            if forced_model in self.models_by_name:
                # Check if allowed based on use_external
                instances = self.models_by_name[forced_model]
                if not use_external:
                    internal_instances = [m for m in instances if not m.external]
                    if not internal_instances:
                        logger.warning(
                            f"FORCE_MODEL={forced_model} ignored because use_external=False and all instances are external"
                        )
                        return []
                logger.info(f"✓ Forcing model name: {forced_model}")
                return [forced_model]
            else:
                logger.warning(f"FORCE_MODEL={forced_model} not found")
                return []

        # No forced model - build list based on use_external with cost ordering
        names = []
        seen = set()

        # Helper to extract model name and add to list
        def add_model_name(model):
            # Find the friendly name from models_by_name (e.g., "fhi-legacy")
            # NOT the internal model path (e.g., "TotallyLegitCo/fighthealthinsurance_model_v0.5")
            model_name = None
            for name, instances in self.models_by_name.items():
                if model in instances:
                    model_name = name
                    break
            if model_name and model_name not in seen:
                names.append(model_name)
                seen.add(model_name)
                return True
            return False

        # Same availability gate as generate_text_backends: this is the list
        # make_appeals fans every appeal out to, and without the gate a
        # backend the sweep had already marked down was called on every run,
        # holding the end of each stream open for its full timeout.
        # Filtered before the slice so healthy backends past the boundary
        # can step up; fails open when everything is marked down.
        internal = self._filter_available(
            self.internal_models_by_cost, "generate-text-names"
        )
        if use_external:
            # Internal + external: take internal first, then the best
            # available external models.
            for model in internal[:6]:
                add_model_name(model)
            for model in self.best_external_models():
                add_model_name(model)
        else:
            # Internal only
            for model in internal:
                add_model_name(model)

        return names

    def full_qa_backends(self, use_external=False) -> list[RemoteModelLike]:
        """
        Return models for handling question-answer pairs for appeal generation.
        Always includes up to three internal FHI models, strongest first.
        When use_external is True, also includes Perplexity for web-informed
        questions when it looks healthy, plus the cheap external generalist
        (google/gemma-4-26B-A4B-it) when none of the chosen internals is a
        healthy general-purpose model.

        Args:
            use_external: Whether to use external models

        Returns:
            List of RemoteModelLike models suitable for QA tasks
        """
        forced = self._get_forced_models("for QA", use_external=use_external)
        if forced:
            return forced

        models: list[RemoteModelLike] = []
        # Always include internal FHI models for question generation. Sorted
        # strongest first BEFORE the slice, so a deployment with more than
        # three internals keeps its best three, not its cheapest three. The
        # sort is stable over the cost order, so ties stay cheapest-first.
        internal = self._general_purpose_only(
            self._filter_available(self.internal_models_by_cost, "full-qa"), "full-qa"
        )
        models += sorted(internal, key=lambda m: -m.quality())[:3]

        if use_external:
            # The cheap external generalist stands in for our own models; it
            # doesn't join them. Question scoring mostly rewards the number
            # and shape of the questions, not model quality, so beside a
            # healthy internal it could win on formatting alone, and the
            # fan-out waits on every task, so it would only add latency and a
            # paid call. So it is added only when none of the internals chosen
            # above is healthy and general-purpose. _external_generalist()
            # skips an instance the health sweep marked down, which would
            # stall the fan-out for the full model timeout. One instance is
            # enough for a concurrent fan-out, as before.
            if not any(
                m.supports_general_instructions() and self._selectable(m)
                for m in models
            ):
                models += self._external_generalist()[:1]
            # Add Perplexity for web-informed questions. Gated like the
            # generalist above: question generation waits on every
            # fanned-out task, so a backed-off Perplexity stalled it for the
            # full timeout.
            if "sonar" in self.models_by_name:
                models += [m for m in self.cheapest("sonar") if self._selectable(m)]

        return models

    def partial_qa_backends(self) -> list[RemoteModelLike]:
        """
        Return models for handling partial question-answer pairs (when we have less context).
        Internal FHI models only: this method has no ``use_external`` gate, so
        it must never include external backends. (It used to append DeepInfra's
        Llama-4-Scout unconditionally; that branch went dead when the model was
        dropped from the catalog and is deliberately not re-pointed.)

        Returns:
            List of RemoteModelLike models suitable for partial QA tasks
        """
        models: list[RemoteModelLike] = []
        # Always include internal FHI models
        models += self._general_purpose_only(
            self._filter_available(self.internal_models_by_cost, "partial-qa"),
            "partial-qa",
        )
        return models

    def full_find_citation_backends(self, use_external=False) -> list[RemoteModelLike]:
        """
        Return models for finding citations.
        Similar to full_qa_backends but only includes Perplexity models.

        Args:
            use_external: Whether to use external models

        Returns:
            List of RemoteModelLike models suitable for citation finding
        """
        if not use_external:
            return []
        return self._citation_backend()

    def partial_find_citation_backends(self) -> list[RemoteModelLike]:
        """
        Return models for finding citations when we have less context.
        Always returns Perplexity models since we're only using
        diagnosis and procedure.

        Returns:
            List of RemoteModelLike models suitable for citation finding with partial context
        """
        return self._citation_backend()

    def _citation_backend(self) -> list[RemoteModelLike]:
        """The Perplexity model citations are found with, or nothing while
        it is down (retired, refused, out of credit, unreachable). Never
        fails open: the citation helpers fall back to the supplemental
        sources, which beats asking a model that cannot answer on every
        appeal."""
        for name in ("sonar-reasoning", "sonar"):
            up = [m for m in self.cheapest(name) if self._selectable(m)]
            if up:
                return up
        return []

    def get_prior_auth_backends(self) -> list[RemoteModelLike]:
        """
        Return models for generating prior authorizations.
        """
        return self._filter_available(self.internal_models_by_cost, "prior-auth")[:3]

    def chat_policy_in_force(
        self, policy: Optional[ChatPolicy]
    ) -> Optional[ChatPolicy]:
        """The chat routing policy the fan-out may follow right now, or None
        to route as if there were none.

        With no internal backend selectable the whole policy is set aside
        (exclusions, caps and delay): the outside models are then the only
        way the turn gets an answer, and failing the turn is worse than any
        of the costs the policy saves.
        """
        if policy is None or policy.narrows_nothing:
            return None
        if not self._healthy_general_internal():
            logger.info(
                "MLRouter: no internal chat backend is selectable; "
                "setting the chat routing policy aside"
            )
            return None
        return policy

    def chat_internal_selectable(self) -> bool:
        """Whether one of our own models can take a chat turn right now
        (instruction-following and not marked down). The live check on our
        reply (chat/reply_gate.py) holds the outside models back only then,
        for the same reason the routing policy is set aside without one."""
        return bool(self._healthy_general_internal())

    def chat_external_delay(self, policy: Optional[ChatPolicy]) -> float:
        """Seconds the chat fan-out holds the outside models back while
        ours answer: the policy's delay, or 0 when it is not in force."""
        in_force = self.chat_policy_in_force(policy)
        return in_force.external_delay_seconds if in_force is not None else 0.0

    def _chat_externals(
        self, policy: Optional[ChatPolicy], explore: bool = True
    ) -> list[RemoteModelLike]:
        """The outside models for a chat turn: the FHI_CHAT_OUTSIDE_MODELS
        roster when it is set (else best_external_models, as before),
        narrowed by the policy when one is in force. The policy never adds
        a model: its learned order only reorders the roster as it is now, so
        a model taken off the roster after the policy was computed is not
        asked, and one added since keeps its roster place after the rest."""
        from django.conf import settings

        in_force = self.chat_policy_in_force(policy)
        roster = list(getattr(settings, "FHI_CHAT_OUTSIDE_MODELS", None) or [])
        if roster:
            names: Optional[list[str]] = None
            if in_force is not None and in_force.outside_order:
                learned = [n for n in in_force.outside_order if n in roster]
                names = learned + [n for n in roster if n not in learned]
            candidates = self.chat_outside_models(names, limit=50)
            externals = self._explore(candidates) if explore else candidates
        else:
            externals = self.best_external_models()
        if in_force is None:
            return externals
        return narrow_externals(externals, in_force)

    def _explore(self, candidates: list[RemoteModelLike]) -> list[RemoteModelLike]:
        """The first CHAT_OUTSIDE_LIMIT of ``candidates`` (the order chat
        asks them in), except that on FHI_CHAT_EXPLORE_RATE of turns the
        second place goes to one of the models further down, so every model
        in the roster keeps being asked often enough for its place in the
        order to be learned."""
        from django.conf import settings

        chosen = candidates[:CHAT_OUTSIDE_LIMIT]
        further = candidates[CHAT_OUTSIDE_LIMIT:]
        rate = float(getattr(settings, "FHI_CHAT_EXPLORE_RATE", 0.2) or 0.0)
        if len(chosen) >= 2 and further and _explore_draw() < rate:
            chosen[1] = further[int(_explore_draw() * len(further)) % len(further)]
        return chosen

    def _chat_lead(self) -> list[RemoteModelLike]:
        """The fhi backend instance(s) that lead the chat fan-out.

        The strongest fhi backend that follows instructions and looks
        healthy, by quality. Equal quality across names goes to the name
        that sorts first, so every pod picks the SAME lead whatever order
        the backends registered in. Each step fails open like
        ``_filter_available``: with no general-purpose fhi backend the
        appeal fine-tune can still lead, and with every candidate marked
        down the strongest one still leads, because a doubled slot on a long
        shot beats no fhi call at all.

        Chosen per INSTANCE, not per name: two backends can share a registry
        name (alpha and the May fine-tune set to the same model path both
        register as one name), and only the strongest of them leads. The
        others under that name take ordinary internal slots. Instances that
        tie on that quality under the chosen name all lead, cheapest first
        (``models_by_name`` keeps each name's backends in cost order). Empty
        when no fhi backend is registered.
        """
        registry_name: dict[int, str] = {}
        fhi: list[RemoteModelLike] = []
        for name, backends in self.models_by_name.items():
            if name.startswith("fhi-"):
                for m in backends:
                    registry_name[id(m)] = name
                    fhi.append(m)
        candidates = self._filter_available(
            self._general_purpose_only(fhi, "chat-fhi"), "chat-fhi"
        )
        if not candidates:
            return []
        strongest = max(m.quality() for m in candidates)
        lead_name = min(
            registry_name[id(m)] for m in candidates if m.quality() == strongest
        )
        return [
            m
            for m in candidates
            if registry_name[id(m)] == lead_name and m.quality() == strongest
        ]

    def get_chat_backends(
        self,
        use_external=False,
        policy: Optional[ChatPolicy] = None,
        explore: bool = True,
    ) -> list[RemoteModelLike]:
        """
        Return models for handling chat interactions.
        Args:
            use_external: Whether to include external models in the fan-out
            policy: Optional chat routing policy (ml/chat_policy.py). It can
                only narrow the external models, and only when use_external
                is on; see chat_policy_in_force for when it is set aside.
            explore: True for a chat turn: CHAT_OUTSIDE_LIMIT roster models,
                one of them sometimes swapped for one further down (see
                _explore). False for the chooser, which compares them all:
                every roster model that can be asked, in order, the same
                answer each time it asks.

        Returns:
            List of RemoteModelLike models suitable for chat tasks
        """
        # Check for forced model override
        forced_models = self._get_forced_models("for chat", use_external=use_external)
        if forced_models:
            return forced_models

        models = []
        # The lead fhi backend is asked twice, for redundancy against a slow
        # pod. It is picked by quality (see _chat_lead), so the doubled slot
        # goes to our strongest model rather than whichever name sorts first.
        lead = self._chat_lead()
        models += lead * 2
        if use_external:
            models += self._chat_externals(policy, explore=explore)
        # Strongest available internals, not cheapest: the cost ordering was
        # picking the 6 CHEAPEST internal backends for chat, which is the
        # wrong end of the list when strong and weak internals coexist. The
        # sort is stable over the cost ordering, so equal-quality models
        # still resolve cheapest-first. The lead already has its two calls,
        # so it is left out here; filtering first and dropping it after keeps
        # the fail-open behaviour judged over the whole internal pool.
        lead_ids = {id(m) for m in lead}
        internal_available = [
            m
            for m in self._general_purpose_only(
                self._filter_available(self.internal_models_by_cost, "chat-internal"),
                "chat-internal",
            )
            if id(m) not in lead_ids
        ]
        internal_to_add = sorted(internal_available, key=lambda m: -m.quality())[:6]
        models += internal_to_add
        logger.debug(
            f"get_chat_backends(use_external={use_external}): {len(models)} models: "
            f"{[str(m) for m in models]}"
        )
        return models

    def get_chat_backends_with_fallback(
        self, use_external=False, policy: Optional[ChatPolicy] = None
    ) -> tuple[list[RemoteModelLike], list[RemoteModelLike]]:
        """
        Return primary and fallback (retry-only) models for chat interactions.

        When ``use_external`` is True the best external models join the
        PRIMARY fan-out alongside the internal backends, not just the retry
        pass: quadratic base scoring still prefers a healthy internal reply,
        but when the internals loop (hard-rejected repeats) or fail, an
        external answer is already in hand instead of costing a full retry
        round trip.

        The fallback list then carries only externals NOT already in the
        primary list -- normally none. The retry pass fans out over BOTH
        lists, so a backend appearing in each would receive four identical
        requests per retry; the externals already get their retry attempt as
        members of the primary list.

        Args:
            use_external: Whether external models participate at all. False
                keeps chat internal-only with no fallback.
            policy: Optional chat routing policy. It narrows the externals
                of both lists the same way (see get_chat_backends).

        Returns:
            Tuple of (primary_models, fallback_models)
        """
        forced_models = self._get_forced_models("for chat", use_external=use_external)
        if forced_models:
            return forced_models, []

        # Reuse get_chat_backends for primary models (allows test mocking to work)
        if policy is None:
            primary_models = self.get_chat_backends(use_external=use_external)
        else:
            primary_models = self.get_chat_backends(
                use_external=use_external, policy=policy
            )

        fallback_models: list[RemoteModelLike] = []
        # When the primary fan-out already has its outside models, the
        # fallback adds none: a second pick could draw another exploration
        # and send the retry to a model the turn never chose.
        primary_has_external = any(
            getattr(m, "external", False) is True for m in primary_models
        )
        if use_external and not primary_has_external:
            # Only externals NOT already in the primary fan-out. build_retry_calls
            # issues two calls per entry of model_backends AND two per entry of
            # fallback_backends, so a backend present in both lists received
            # four byte-identical requests per retry -- doubled paid spend and
            # rate-limit pressure on exactly the turns that already failed,
            # with no added diversity.
            already_primary = {id(m) for m in primary_models}
            fallback_models = [
                m for m in self._chat_externals(policy) if id(m) not in already_primary
            ]

        return primary_models, fallback_models

    async def summarize_chat_history(
        self, history: list[dict], max_messages: int = 10
    ) -> Optional[str]:
        """
        Summarize chat history when it gets too long.
        This helps reduce context size and avoid timeouts.

        Args:
            history: List of chat messages with 'role' and 'content' keys
            max_messages: Maximum number of recent messages to keep unsummarized.
                         If 0, summarize all messages in history.

        Returns:
            Summary string of older messages, or None if summarization fails
        """
        if not history:
            return None

        if max_messages > 0 and len(history) <= max_messages:
            return None

        # Get messages to summarize (older ones, or all if max_messages=0)
        if max_messages == 0:
            to_summarize = history
        else:
            to_summarize = history[:-max_messages]

        if not to_summarize:
            return None

        # Build a text representation, ensuring proper role alternation for clarity
        history_text = ""
        last_role = None
        for msg in to_summarize:
            role = msg.get("role", "unknown")
            # Normalize role names for consistency
            if role in ("assistant", "agent", "system"):
                role = "assistant"
            content = msg.get("content", "")
            if content:
                # Truncate very long messages
                if len(content) > 500:
                    content = content[:500] + "..."
                # If same role as last, merge the content
                if role == last_role and history_text:
                    history_text = history_text.rstrip("\n") + f" {content}\n"
                else:
                    history_text += f"{role}: {content}\n"
                last_role = role

        if not history_text:
            return None

        # Use internal models to summarize
        if not self.internal_models_by_cost:
            logger.warning(
                "summarize_chat_history called but no internal models are available; "
                "skipping summarization and returning None."
            )
            return None
        models = self._general_purpose_only(
            self._filter_available(
                self.internal_models_by_cost, "summarize-chat-history"
            ),
            "summarize-chat-history",
        )[:2]
        for m in models:
            try:
                summary = await m._infer_no_context(
                    system_prompts=[
                        "You are a helpful assistant summarizing a conversation for context. "
                        "Be concise but capture key details about the health insurance issue, "
                        "denied items, and any relevant medical information discussed."
                    ],
                    prompt=f"Summarize this conversation for context:\n\n{history_text}\n\n"
                    "Focus on: what was denied, why, any medical details, and what the user needs help with.",
                    timeout=ml_task_timeout("summarize"),
                )
                if summary and len(summary) > 10:
                    return summary
            except Exception as e:
                logger.warning(f"Error summarizing chat history with {m}: {e}")
                continue

        return None

    def general_purpose_internal_models(self) -> list[RemoteModelLike]:
        """Internal backends that follow instructions, cheapest-first.

        The default pool for one-shot inference helpers (entity extraction,
        document/policy parsing, chat synthesis). Fails open, so it is never
        empty when ``internal_models_by_cost`` isn't.
        """
        return self._general_purpose_only(
            self.internal_models_by_cost, "internal-inference"
        )

    def best_internal_model(
        self, *, general_only: bool = True
    ) -> Optional[RemoteModelLike]:
        """Return the highest-quality internal model available, or None.

        ``general_only`` (the default) skips the appeal-text fine-tunes,
        which is what every instruction-following caller wants. Appeal and
        prior-auth GENERATION must pass ``general_only=False`` -- there the
        fine-tune is the point.
        """
        if not self.internal_models_by_cost:
            return None
        candidates: Sequence[RemoteModelLike] = self.internal_models_by_cost
        if general_only:
            candidates = self._general_purpose_only(candidates, "best-internal")
        return max(candidates, key=lambda m: m.quality())

    def backends_for_name(self, name: str) -> list[RemoteModelLike]:
        """The instances registered under ``name``, healthy first (see
        healthy_first)."""
        return self.healthy_first(self.models_by_name.get(name, []))

    def healthy_first(
        self, instances: Sequence[RemoteModelLike]
    ) -> list[RemoteModelLike]:
        """``instances`` with those that look healthy first (their order
        otherwise kept), so a call by name goes to one that can answer
        before one the health signals have down. Every instance stays
        listed, since a healthy one may still fail, and one whose signals
        cannot be read counts as healthy."""

        def down(model: RemoteModelLike) -> bool:
            try:
                return not self._selectable(model)
            except Exception:
                return False

        return sorted(instances, key=down)

    def cheapest(self, name: str) -> list[RemoteModelLike]:
        try:
            return [self.models_by_name[name][0]]
        except (KeyError, IndexError):
            return []

    def summarize_backends(self, use_external: bool) -> list[RemoteModelLike]:
        """The models ``summarize`` tries, in the order it tries them.

        Our own general-purpose backends that look healthy come first,
        strongest first. The cheap external generalist comes next, only when
        ``use_external`` allows it. Last comes the same fail-open internal
        pool ``summarize`` has always used, minus what is already listed, so
        a stale health signal can't leave a summary with nothing to try.

        Never calls a model, so a staff page can show the order without
        spending an inference. It reads only cached health signals: each
        model's own ``is_available()`` and, for backends without a live
        signal, the last ``health_status`` sweep. Like any routed request,
        the first read on a pod whose background health sweep hasn't started
        yet starts it, and that sweep probes the backends' ``/models``
        endpoints in a background thread.
        """
        # Strict: only internals that follow instructions AND look healthy.
        # The fail-open pool can hold the appeal-only fhi-legacy, whose
        # digit soup can pass summarize()'s length check, so it must not go
        # ahead of the external generalist.
        head = self._healthy_general_internal()
        # A DeepInfra-only deployment has no internal backend at all, so there
        # the external generalist is the whole list; without it summarize()
        # would quietly return None. It stays behind use_external so a caller
        # summarizing patient data for an opt-out denial never sends it out.
        external = self._external_generalist() if use_external else []
        # Last resort: internals the health signals marked down, or the
        # appeal-only fine-tune when it is all we have.
        fallback = self._general_purpose_only(
            self._filter_available(self.internal_models_by_cost, "summarize"),
            "summarize",
        )
        listed = {id(m) for m in head}
        return head + external + [m for m in fallback if id(m) not in listed]

    async def summarize(
        self,
        title: Optional[str],
        text: Optional[str],
        abstract: Optional[str] = None,
        *,
        use_external: bool = True,
        max_input_chars: int = 1000,
    ) -> Optional[str]:
        """Summarize ``text``/``abstract`` for use in an appeal.

        ``use_external`` gates whether the cheap external generalist may be
        used. It defaults to True because the original callers summarize
        PUBLIC article text (PubMed/citation bodies). Callers summarizing
        patient data (e.g. a long denial letter) MUST pass
        ``use_external=denial.use_external`` so an opt-out denial never routes
        PHI to an external provider. ``max_input_chars`` caps how much source
        text is fed to the model (default 1000 for short article snippets;
        denial-text summarization passes a larger cap so the summary actually
        reflects the whole letter).
        """
        # Our strongest healthy internal first, then the cheap DeepInfra
        # generalist (only when use_external allows it and it looks healthy),
        # then the internals marked down. A DeepInfra-only deployment has an
        # empty internal pool, so there the generalist is the only model and
        # summarize() still works rather than silently returning None. See
        # summarize_backends().
        models = self.summarize_backends(use_external)
        abstract_optional = ""
        text_optional = ""
        if abstract is not None:
            abstract_optional = (
                f"--- Current abstract: {abstract[0:max_input_chars]} ---"
            )
        if text is not None:
            text_optional = f"--- Full-ish article text: {text[0:max_input_chars]} ---"
        system_prompts = [
            "You are a helpful assistant summarizing article(s) for a person or other LLM writing an appeal. Be very concise."
        ]
        instructions = (
            "If present in the input include a list of the most relevant "
            "articles referenced (with PMID / DOIs or links if present in the "
            "input). If multiple studies prefer US studies then generic "
            "non-country specific and then other countries. We're focused on "
            "helping American patients and providers."
        )
        # Each attempt must carry SOURCE TEXT. The abstract-only retry below is
        # skipped when there is no abstract: with abstract=None its prompt would
        # be the bare instruction with nothing to summarize, and a model answers
        # that with a refusal or an invention rather than failing. Callers can't
        # tell the difference (they only check for a falsy result), so for
        # denial-text summarization that fabricated text would be cached in
        # denial_text_summary and substituted for the user's actual letter on
        # every later generation -- appeals written about a denial that isn't
        # theirs. Both denial callers pass abstract=None.
        attempts: list[str] = [
            f"Summarize the following {title} for use in a health insurance "
            f"appeal: {abstract_optional}{text_optional}. {instructions}"
        ]
        if abstract_optional and text_optional:
            # Only meaningful when the first attempt had MORE than the abstract;
            # otherwise it is a byte-for-byte repeat of it.
            attempts.append(
                f"Summarize the following {title} for use in a health insurance "
                f"appeal: {abstract_optional}. {instructions}"
            )
        return await self._run_summarizer(models, system_prompts, attempts)

    async def _run_summarizer(
        self,
        models: list[RemoteModelLike],
        system_prompts: list[str],
        attempts: list[str],
    ) -> Optional[str]:
        """Try each prompt in ``attempts`` on each model in turn; return the
        first non-trivial summary."""
        for m in models:
            for prompt in attempts:
                try:
                    r = await m._infer_no_context(
                        system_prompts=system_prompts,
                        prompt=prompt,
                        timeout=ml_task_timeout("summarize"),
                    )
                except Exception as e:
                    # Per-model guard: subclasses with _propagate_http_errors
                    # re-raise unexpected statuses, and without this one 500 from
                    # the first backend would skip every remaining fallback.
                    logger.opt(exception=True).warning(
                        f"summarize: {m} failed, trying the next model: {e}"
                    )
                    break
                # Treat a blank/trivial response as a failure and keep going
                # rather than handing it back: callers substitute this for the
                # source text, so "   " would silently become the thing we
                # summarize FROM. Same threshold as summarize_chat_history.
                if r is not None and len(r.strip()) > 10:
                    return r
                if r is not None:
                    logger.debug(
                        f"summarize: {m} returned a trivial result "
                        f"({len(r.strip())} chars); trying the next option"
                    )
        return None

    DENIAL_SUMMARY_SYSTEM_PROMPT = (
        "You condense health insurance denial letters so an appeal can be "
        "written from the condensed version. Keep every fact an appeal would "
        "need, drop boilerplate, and never add anything that is not in the "
        "letter."
    )

    async def summarize_denial_letter(
        self,
        text: str,
        *,
        use_external: bool,
        max_input_chars: int,
    ) -> Optional[str]:
        """Condense a long denial letter for use as the appeal prompt's
        denial text.

        Distinct from ``summarize`` (which is framed for PubMed articles:
        PMIDs, DOIs, a preference for US studies) because the letter is not
        an article and what must survive is different: the denied service and
        codes, the payer's stated reasons and cited policies, dates,
        identifiers, and how/when to appeal. ``use_external`` MUST be the
        denial's own opt-in since the letter carries PHI.
        """
        if not text or not text.strip():
            return None
        models = self.summarize_backends(use_external)
        truncated = len(text) > max_input_chars
        body = text[0:max_input_chars]
        truncation_note = (
            "\n[The letter was cut off here; summarize what is shown.]"
            if truncated
            else ""
        )
        prompt = (
            "Condense the following health insurance denial letter for use in "
            "an appeal. Keep, verbatim where possible:\n"
            "- the denied service, procedure, or drug, with any CPT/HCPCS/NDC "
            "codes\n"
            "- the diagnosis and any ICD codes\n"
            "- the payer's stated reason(s) for denial and any policy, "
            "guideline, or criteria it cites\n"
            "- dates of service and of the decision\n"
            "- claim, member, plan, and group identifiers, and the insurer's "
            "name\n"
            "- the appeal deadline and how to appeal (address, fax, portal, "
            "phone)\n"
            "Leave out generic notices, marketing, and repeated disclaimers. "
            "Do not add facts that are not in the letter. Output plain text "
            "with no preamble.\n\n"
            f"--- Denial letter:\n{body}{truncation_note}\n---"
        )
        return await self._run_summarizer(
            models, [self.DENIAL_SUMMARY_SYSTEM_PROMPT], [prompt]
        )

    def working(self) -> bool:
        """Return if we have candidates to route to. (TODO: Check they're alive)"""
        return len(self.all_models_by_cost) > 0

    async def probe_all_models(
        self, per_model_timeout: float = 20.0
    ) -> List[Tuple[str, bool, Optional[str]]]:
        """Probe every registered backend with a tiny "Hello" inference.

        Intended to run once at startup to surface misconfigured or
        over-quota backends in the logs. Unlike the free liveness check
        (``model_is_ok``), each probe makes a real -- though tiny and
        one-off -- inference call, so this should not be wired into any
        per-request path.

        Returns a list of ``(name, ok, error)`` tuples. Failing backends are
        logged at WARNING; this never raises.
        """
        # Context-only backends (e.g. Perplexity) are included on purpose --
        # those are exactly the ones whose quota/billing failures (HTTP 401)
        # we want to catch early. Dedup by identity since a backend can appear
        # in more than one pool.
        seen: set[int] = set()
        models: List[RemoteModelLike] = []
        for m in list(self.all_models_by_cost) + list(self.context_only_models_by_cost):
            if id(m) not in seen:
                seen.add(id(m))
                models.append(m)

        if not models:
            logger.info("Model startup probe: no backends registered to probe")
            return []

        async def _probe_one(m: RemoteModelLike) -> Tuple[str, bool, Optional[str]]:
            name = str(m)
            try:
                ok, err = await m.probe(timeout=per_model_timeout)
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            if ok:
                logger.debug(f"Model startup probe OK: {name}")
            else:
                logger.warning(f"Model startup probe FAILED: {name} ({err})")
            return (name, ok, err)

        results = await asyncio.gather(*[_probe_one(m) for m in models])
        ok_count = sum(1 for _, ok, _ in results if ok)
        failures = [(n, e) for n, ok, e in results if not ok]
        if failures:
            failure_summary = "; ".join(f"{n}: {e}" for n, e in failures)
            logger.warning(
                f"Model startup probe complete: {ok_count}/{len(results)} "
                f"backends responded. Failures: {failure_summary}"
            )
        else:
            logger.info(
                f"Model startup probe complete: all {ok_count} backends responded"
            )
        return results


def appeal_backup_names(
    candidates: Sequence[str], primary_names: Sequence[str]
) -> List[str]:
    """The names the appeals backup pass calls, in order.

    ``candidates`` is what ``generate_text_backend_names`` returns for the
    denial's ``use_external``, and ``primary_names`` is the internal-only list
    the primary pass already called. The backup never repeats one of those.
    So for an opt-out denial it is empty and ``make_appeals`` skips the stage,
    and for an opt-in one it holds the external backends. ``make_appeals``
    and the staff routing overview both use this, so the page lists the
    backup pass requests actually run.
    """
    already = set(primary_names)
    return [name for name in candidates if name not in already]


# Lazy singleton - initialized on first access
_ml_router_instance: Optional[MLRouter] = None
_ml_router_lock = threading.Lock()


def _get_ml_router() -> MLRouter:
    """Get or create the singleton MLRouter instance (lazy initialization).

    Locked: first access comes from concurrent request threads (and the
    startup probe), and an unlocked check-then-create races into building
    TWO routers -- each with its own backend instances, cooldown state, and
    health view, with half the callers holding a router the health sweep
    never updates again.
    """
    global _ml_router_instance
    if _ml_router_instance is None:
        with _ml_router_lock:
            if _ml_router_instance is None:
                _ml_router_instance = MLRouter()
    return _ml_router_instance


# Property-like access for backward compatibility
class _MLRouterProxy:
    def __getattr__(self, name):
        return getattr(_get_ml_router(), name)


ml_router = _MLRouterProxy()

import asyncio
import os
from typing import List, Optional, Sequence, Tuple

from loguru import logger

from fighthealthinsurance.ml.ml_models import *


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

    def __init__(self):
        # Initialize instance attributes to avoid mutable class-level state
        self.models_by_name = {}
        self.internal_models_by_cost = []
        self.all_models_by_cost = []
        self.external_models_by_cost = []
        self.context_only_models_by_cost = []
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
        for backend in candidate_model_backends:
            try:
                models = backend.models()
                logger.debug(f"MLRouter: {backend} provided {len(models)} models")
                for m in models:
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
                    # building context such as citations, web-informed research,
                    # and QA question generation (QA is a form of context
                    # building). They must never appear in the general generation
                    # pools but remain reachable by name via models_by_name for
                    # the context-building and QA methods.
                    if m.model.context_only:
                        building_context_only_models_by_cost.append(m)
                    else:

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
                logger.warning(f"Skipping {backend} due to {e} of {type(e)}")
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
        logger.info(
            f"MLRouter initialized with {len(self.all_models_by_cost)} total models, "
            f"{len(self.internal_models_by_cost)} internal, {len(self.external_models_by_cost)} external, "
            f"{len(self.context_only_models_by_cost)} context-only"
        )
        logger.info(f"All loaded models: {[str(m) for m in self.all_models_by_cost]}")
        logger.debug(
            f"Built {self} with i:{self.internal_models_by_cost} a:{self.all_models_by_cost}"
        )

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
        raw = os.getenv("ENABLED_REMOTE_MODELS")
        if not raw or not raw.strip():
            return None
        names = {n.strip() for n in raw.split(",") if n.strip()}
        return names or None

    @staticmethod
    def _keep_single_groq(models: Sequence[RemoteModelLike]) -> list[RemoteModelLike]:
        """
        Return the models list but with at most one Groq backend to avoid double fanout.
        Goal is to reduce load on Groq due to usage limits.
        """
        seen_groq = False
        filtered: list[RemoteModelLike] = []
        for m in models:
            if isinstance(m, RemoteGroq):
                if seen_groq:
                    continue
                seen_groq = True
            filtered.append(m)
        return filtered

    def best_external_models(self, limit: int = 3) -> list[RemoteModelLike]:
        """Return up to ``limit`` external models, best-quality first, limited to
        those currently available.

        "Best" means highest ``quality()`` (descending); because
        ``external_models_by_cost`` is cost-ordered and the sort is stable,
        cheaper models win ties. Availability is judged from cheap, non-blocking
        signals only — never a live network probe on the request path (see
        :meth:`_external_selectable`): the model's in-memory ``is_available()``
        plus, for backends without a live signal, the last cached
        ``health_status`` sweep. At most one Groq backend is kept (matching the
        rest of the router) to avoid double fan-out.

        Replaces the previous "cheapest N external" slices so that, instead of
        fanning out across every external backend, we route to the strongest
        few that are up.
        """
        available = [
            m for m in self.external_models_by_cost if self._external_selectable(m)
        ]
        available = self._keep_single_groq(available)
        best = sorted(available, key=lambda m: -m.quality())
        return best[:limit]

    def _external_selectable(self, model: RemoteModelLike) -> bool:
        """Whether an external model should be offered for fan-out, using only
        cheap, non-blocking signals — no live network calls in the router.

        * ``is_available()`` — the model's own in-memory signal (paid providers
          report config + rate-limit back-off; others fail open).
        * the last cached ``health_status`` sweep — consulted only for models
          that lack a live signal (``health_checked_live`` is False, e.g.
          DeepInfra, which the sweep probes over the network). Models the last
          sweep marked unhealthy are skipped; ones it hasn't checked yet fail
          open, so we assume a backend is available until a real check says
          otherwise.
        """
        if not model.is_available():
            return False
        if not model.health_checked_live:
            # Imported lazily to avoid a circular import (health_status imports
            # the router). model_ok() is a lock-free cache read.
            from fighthealthinsurance.ml.health_status import health_status

            if health_status.model_ok(model) is False:
                return False
        return True

    def _get_forced_models(
        self, task_description: str = "", *, use_external: bool = True
    ) -> Optional[list[RemoteModelLike]]:
        """Check for FORCE_MODEL environment variable and return forced models if set.

        Args:
            task_description: Optional description for logging (e.g., "for text generation")

        Returns:
            List of forced models if FORCE_MODEL is set and models are found, None otherwise
        """
        forced_model = os.getenv("FORCE_MODEL")
        if not forced_model:
            return None

        logger.info(
            f"FORCE_MODEL={forced_model} {task_description}; "
            f"available: {[type(m).__name__ for m in self.all_models_by_cost]}"
        )

        if forced_model == "groq":
            groq_models = [
                m for m in self.all_models_by_cost if isinstance(m, RemoteGroq)
            ]
            if groq_models:
                if not use_external:
                    logger.warning(
                        f"FORCE_MODEL=groq ignored {task_description} because use_external=False"
                    )
                    return None
                logger.info(
                    f"✓ Forcing {len(groq_models)} groq models {task_description}: {[getattr(m, 'model', type(m).__name__) for m in groq_models]}"
                )
                return self._keep_single_groq(groq_models)
            else:
                logger.warning(
                    f"No groq models found! Available model types: {[type(m).__name__ for m in self.all_models_by_cost]}"
                )
        elif forced_model in self.models_by_name:
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
            return self.all_models_by_cost
        else:
            return self.internal_models_by_cost

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

        # First try to find specific text generation models
        if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
            return self.cheapest("meta-llama/Llama-4-Scout-17B-16E-Instruct")

        # Fall back to internal models, optionally appending external if allowed
        models: list[RemoteModelLike] = []
        if self.internal_models_by_cost:
            models += self.internal_models_by_cost[:6]
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
                if not m.external or self._external_selectable(m)
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
        1. Router returns ["fhi-2025", "sonar"]
        2. Caller looks up models_by_name["fhi-2025"] → [server1, server2, server3]
        3. Caller tries server1, if fails → server2, if fails → server3

        Args:
            use_external: Whether to include external models

        Returns:
            List of model names (not instances) to use for text generation
        """
        # Check for forced model override
        forced_model = os.getenv("FORCE_MODEL")
        if forced_model:
            logger.info(f"FORCE_MODEL={forced_model} for text generation")

            if forced_model == "groq":
                if not use_external:
                    logger.warning(
                        "FORCE_MODEL=groq ignored for text generation because use_external=False"
                    )
                    return []
                # Find groq model names
                groq_names = []
                for name, instances in self.models_by_name.items():
                    if any(isinstance(m, RemoteGroq) for m in instances):
                        groq_names.append(name)
                if groq_names:
                    # Return only first groq model name to avoid double-fanout
                    logger.info(f"✓ Forcing groq model name: {groq_names[0]}")
                    return [groq_names[0]]
                else:
                    logger.warning("No groq models found!")
                    return []

            elif forced_model in self.models_by_name:
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

        if use_external:
            # Internal + external: take internal first, then the best
            # available external models.
            for model in self.internal_models_by_cost[:6]:
                add_model_name(model)
            for model in self.best_external_models():
                add_model_name(model)
        else:
            # Internal only
            for model in self.internal_models_by_cost:
                add_model_name(model)

        return names

    def full_qa_backends(self, use_external=False) -> list[RemoteModelLike]:
        """
        Return models for handling question-answer pairs for appeal generation.
        Always includes internal FHI models. When use_external is True, also
        includes external models like Llama Scout and Perplexity.

        Args:
            use_external: Whether to use external models

        Returns:
            List of RemoteModelLike models suitable for QA tasks
        """
        forced = self._get_forced_models("for QA", use_external=use_external)
        if forced:
            return forced

        models: list[RemoteModelLike] = []
        # Always include internal FHI models for question generation
        models += self.internal_models_by_cost[:3]

        if use_external:
            # Add Llama Scout model if available
            if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
                models += self.cheapest("meta-llama/Llama-4-Scout-17B-16E-Instruct")
            # Add Perplexity for web-informed questions
            if "sonar" in self.models_by_name:
                models += self.cheapest("sonar")

        return models

    def partial_qa_backends(self) -> list[RemoteModelLike]:
        """
        Return models for handling partial question-answer pairs (when we have less context).
        Includes internal FHI models and Llama Scout if available.

        Returns:
            List of RemoteModelLike models suitable for partial QA tasks
        """
        models: list[RemoteModelLike] = []
        # Always include internal FHI models
        models += self.internal_models_by_cost
        # Add Llama Scout model if available
        if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
            models += self.cheapest("meta-llama/Llama-4-Scout-17B-16E-Instruct")
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

        # Only use Perplexity models for citations
        if "sonar-reasoning" in self.models_by_name:
            return self.cheapest("sonar-reasoning")
        if "sonar" in self.models_by_name:
            return self.cheapest("sonar")

        return []

    def partial_find_citation_backends(self) -> list[RemoteModelLike]:
        """
        Return models for finding citations when we have less context.
        Always returns Perplexity models since we're only using
        diagnosis and procedure.

        Returns:
            List of RemoteModelLike models suitable for citation finding with partial context
        """
        # Only use Perplexity models for citations
        if "sonar-reasoning" in self.models_by_name:
            return self.cheapest("sonar-reasoning")
        if "sonar" in self.models_by_name:
            return self.cheapest("sonar")

        return []

    def get_prior_auth_backends(self) -> list[RemoteModelLike]:
        """
        Return models for generating prior authorizations.
        """
        return self.internal_models_by_cost[:3]

    def get_chat_backends(self, use_external=False) -> list[RemoteModelLike]:
        """
        Return models for handling chat interactions.
        Args:
            use_external: Whether to include external models as fallback

        Returns:
            List of RemoteModelLike models suitable for chat tasks
        """
        # Check for forced model override
        forced_models = self._get_forced_models("for chat", use_external=use_external)
        if forced_models:
            return forced_models

        models = []
        # Try each fhi model twice
        fhi_models = [
            name for name in self.models_by_name.keys() if name.startswith("fhi-")
        ]
        if fhi_models:
            models += self.models_by_name[fhi_models[0]] * 2
        if use_external:
            models += self.best_external_models()
        internal_to_add = self.internal_models_by_cost[:6]
        models += internal_to_add
        logger.debug(
            f"get_chat_backends(use_external={use_external}): {len(models)} models: "
            f"{[str(m) for m in models]}"
        )
        return models

    def get_chat_backends_with_fallback(
        self, use_external=False
    ) -> tuple[list[RemoteModelLike], list[RemoteModelLike]]:
        """
        Return primary (FHI) and fallback (external) models for chat interactions.
        The fallback models are only returned if use_external is True.

        Args:
            use_external: Whether to include external models as fallback

        Returns:
            Tuple of (primary_models, fallback_models)
            - primary_models: FHI/internal models to try first
            - fallback_models: External models to try if primary fails (empty if use_external=False)
        """
        forced_models = self._get_forced_models("for chat", use_external=use_external)
        if forced_models:
            return forced_models, []

        # Reuse get_chat_backends for primary models (allows test mocking to work)
        primary_models = self.get_chat_backends(use_external=False)

        fallback_models: list[RemoteModelLike] = []
        if use_external:
            # Prefer the best available external models for chat fallback.
            fallback_models = self.best_external_models()

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
        models = self.internal_models_by_cost[:2]
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
                )
                if summary and len(summary) > 10:
                    return summary
            except Exception as e:
                logger.warning(f"Error summarizing chat history with {m}: {e}")
                continue

        return None

    def best_internal_model(self) -> Optional[RemoteModelLike]:
        """Return the highest-quality internal model available, or None."""
        if not self.internal_models_by_cost:
            return None
        return max(self.internal_models_by_cost, key=lambda m: m.quality())

    def cheapest(self, name: str) -> list[RemoteModelLike]:
        try:
            return [self.models_by_name[name][0]]
        except (KeyError, IndexError):
            return []

    async def summarize(
        self, title: Optional[str], text: Optional[str], abstract: Optional[str] = None
    ) -> Optional[str]:
        models: list[RemoteModelLike] = []
        if "google/gemma-3-27b-it" in self.models_by_name:
            models = (
                self.models_by_name["google/gemma-3-27b-it"]
                + self.internal_models_by_cost
            )
        else:
            models = self.internal_models_by_cost
        abstract_optional = ""
        text_optional = ""
        if abstract is not None:
            abstract_optional = f"--- Current abstract: {abstract[0:1000]} ---"
        if text is not None:
            text_optional = f"--- Full-ish article text: {text[0:1000]} ---"
        for m in models:
            r = await m._infer_no_context(
                system_prompts=[
                    "You are a helpful assistant summarizing article(s) for a person or other LLM wriitng an appeal. Be very concise."
                ],
                prompt=f"Summarize the following {title} for use in a health insurance appeal: {abstract_optional}{text_optional}. If present in the input include a list of the most relevant articles referenced (with PMID / DOIs or links if present in the input). If multile studies prefer US studies then generic non-country specific and then other countries. We're focused on helping american patients and providers.",
            )
            if r is not None:
                return r
            # Try again with only the abstract
            r = await m._infer_no_context(
                system_prompts=[
                    "You are a helpful assistant summarizing article(s) for a person or other LLM wriitng an appeal. Be very concise."
                ],
                prompt=f"Summarize the following {title} for use in a health insurance appeal: {abstract_optional}. If present in the input include a list of the most relevant articles referenced (with PMID / DOIs or links if present in the input). If multile studies prefer US studies then generic non-country specific and then other countries. We're focused on helping american patients and providers.",
            )
            if r is not None:
                return r
        return None

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


# Lazy singleton - initialized on first access
_ml_router_instance: Optional[MLRouter] = None


def _get_ml_router() -> MLRouter:
    """Get or create the singleton MLRouter instance (lazy initialization)."""
    global _ml_router_instance
    if _ml_router_instance is None:
        _ml_router_instance = MLRouter()
    return _ml_router_instance


# Property-like access for backward compatibility
class _MLRouterProxy:
    def __getattr__(self, name):
        return getattr(_get_ml_router(), name)


ml_router = _MLRouterProxy()

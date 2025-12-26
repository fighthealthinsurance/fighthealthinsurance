import asyncio
from typing import List
from loguru import logger
from typing import Optional

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

    def __init__(self):
        # Initialize instance attributes to avoid mutable class-level state
        self.models_by_name = {}
        self.internal_models_by_cost = []
        self.all_models_by_cost = []
        self.external_models_by_cost = []
        logger.debug(f"Starting model 'router'")
        building_internal_models_by_cost = []
        building_external_models_by_cost = []
        building_all_models_by_cost = []
        building_models_by_name: dict[str, List[ModelDescription]] = {}
        for backend in candidate_model_backends:
            logger.debug(f"Considering {backend}")
            try:
                models = backend.models()
                logger.debug(f"{backend} gave us {models}")
                for m in models:
                    logger.debug(f"Adding {m} from {backend}")
                    if m.model is None:
                        m.model = backend(model=m.internal_name)
                        logger.debug(f"Built {m.model}")
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
                    logger.debug(f"Added {m}")
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
        logger.debug(
            f"Built {self} with i:{self.internal_models_by_cost} a:{self.all_models_by_cost}"
        )

    def entity_extract_backends(self, use_external) -> list[RemoteModelLike]:
        """Backends for entity extraction."""
        if use_external:
            return self.all_models_by_cost
        else:
            return self.internal_models_by_cost

    def generate_text_backends(self) -> list[RemoteModelLike]:
        """Return models for text generation tasks like prior authorization and ongoing chat."""
        # First try to find specific text generation models
        if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
            return self.cheapest("meta-llama/Llama-4-Scout-17B-16E-Instruct")

        # Fall back to any available models
        return self.all_models_by_cost[:6] if self.all_models_by_cost else []

    def full_qa_backends(self, use_external=False) -> list[RemoteModelLike]:
        """
        Return models for handling question-answer pairs for appeal generation.
        If use_external is True, includes external models like Perplexity and Llama,
        otherwise returns an empty list (no models).

        Args:
            use_external: Whether to use external models

        Returns:
            List of RemoteModelLike models suitable for QA tasks
        """
        if not use_external:
            return []

        # Add Llama Scout model if available
        if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
            return self.cheapest("meta-llama/Llama-4-Scout-17B-16E-Instruct")

        return []

    def partial_qa_backends(self) -> list[RemoteModelLike]:
        """
        Return models for handling partial question-answer pairs (when we have less context).
        Always returns Perplexity models since we're only using
        diagnosis and procedure.

        Returns:
            List of RemoteModelLike models suitable for partial QA tasks
        """
        # Add Llama Scout model if available
        if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
            return self.cheapest("meta-llama/Llama-4-Scout-17B-16E-Instruct")
        return []

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
        models = []
        # Try each fhi model twice
        fhi_models = [
            name for name in self.models_by_name.keys() if name.startswith("fhi-")
        ]
        if fhi_models:
            models += self.models_by_name[fhi_models[0]] * 2
        if use_external:
            models += self.external_models_by_cost[:2]
        models += self.internal_models_by_cost[:6]
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
        # Reuse get_chat_backends for primary models (allows test mocking to work)
        primary_models = self.get_chat_backends(use_external=False)

        fallback_models: list[RemoteModelLike] = []
        if use_external:
            # Get external models sorted by quality for fallback
            external_models = [m for m in self.all_models_by_cost if m.external]
            # Prefer higher quality models for chat fallback
            fallback_models = sorted(external_models, key=lambda m: -m.quality())[:4]

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

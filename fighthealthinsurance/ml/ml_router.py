import asyncio
from typing import List
from loguru import logger
from typing import Optional

from fighthealthinsurance.ml.ml_models import *


class MLRouter(object):
    """
    Tool to route our requests most cheapily.
    """

    models_by_name: dict[str, List[RemoteModelLike]] = {}
    internal_models_by_cost: List[RemoteModelLike] = []
    all_models_by_cost: List[RemoteModelLike] = []

    def __init__(self):
        logger.debug(f"Starting model 'router'")
        building_internal_models_by_cost = []
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
                        building_internal_models_by_cost = (
                            building_internal_models_by_cost
                        )
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
        logger.debug(
            f"Built {self} with i:{self.internal_models_by_cost} a:{self.all_models_by_cost}"
        )

    def entity_extract_backends(self, use_external) -> list[RemoteModelLike]:
        """Backends for entity extraction."""
        if use_external:
            return self.all_models_by_cost
        else:
            return self.internal_models_by_cost

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

        qa_models = []

        # Add Perplexity models if available
        if "sonar-reasoning" in self.models_by_name:
            qa_models.extend(self.models_by_name["sonar-reasoning"])
        if "deepseek" in self.models_by_name:
            qa_models.extend(self.models_by_name["deepseek"])

        # Add Llama Scout model if available
        if "meta-llama/Llama-4-Scout-17B-16E-Instruct" in self.models_by_name:
            qa_models.extend(
                self.models_by_name["meta-llama/Llama-4-Scout-17B-16E-Instruct"]
            )

        return qa_models

    def partial_qa_backends(self) -> list[RemoteModelLike]:
        """
        Return models for handling partial question-answer pairs (when we have less context).
        Always returns Perplexity models regardless of the external flag.

        Returns:
            List of RemoteModelLike models suitable for partial QA tasks
        """
        partial_models = []

        # Add Perplexity models if available
        if "sonar-reasoning" in self.models_by_name:
            partial_models.extend(self.models_by_name["sonar-reasoning"])
        if "deepseek" in self.models_by_name:
            partial_models.extend(self.models_by_name["deepseek"])

        return partial_models

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

        citation_models = []

        # Only use Perplexity models for citations
        if "sonar-reasoning" in self.models_by_name:
            citation_models.extend(self.models_by_name["sonar-reasoning"])
        if "deepseek" in self.models_by_name:
            citation_models.extend(self.models_by_name["deepseek"])

        return citation_models

    def partial_find_citation_backends(self) -> list[RemoteModelLike]:
        """
        Return models for finding citations when we have less context.
        Always returns Perplexity models regardless of the external flag.

        Returns:
            List of RemoteModelLike models suitable for citation finding with partial context
        """
        citation_models = []

        # Only use Perplexity models for citations
        if "sonar-reasoning" in self.models_by_name:
            citation_models.extend(self.models_by_name["sonar-reasoning"])
        if "deepseek" in self.models_by_name:
            citation_models.extend(self.models_by_name["deepseek"])

        return citation_models

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
            r = await m._infer(
                system_prompts=[
                    "You are a helpful assistant summarizing article(s) for a person or other LLM wriitng an appeal. Be very concise."
                ],
                prompt=f"Summarize the following {title} for use in a health insurance appeal: {abstract_optional}{text_optional}. If present in the input include a list of the most relevant articles referenced (with PMID / DOIs or links if present in the input). If multile studies prefer US studies then generic non-country specific and then other countries. We're focused on helping american patients and providers.",
            )
            if r is not None:
                return r
            # Try again with only the abstract
            r = await m._infer(
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


ml_router = MLRouter()

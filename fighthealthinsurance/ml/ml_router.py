import asyncio
from typing import List
from loguru import logger

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

    def summarize(
        self, text: Optional[str], query: str, abstract: Optional[str]
    ) -> Optional[str]:
        models: list[RemoteModelLike] = []
        if "meta-llama/Llama-3.3-70B-Instruct-Turbo" in self.models_by_name:
            models = self.models_by_name["meta-llama/Llama-3.3-70B-Instruct-Turbo"]
        else:
            models = self.all_models_by_cost
        abstract_optional = ""
        text_optional = ""
        if abstract is not None:
            abstract_optional = f"--- Current abstract: {abstract} ---"
        if text is not None:
            text_optional = f"--- Full-ish article text: {text} ---"
        for m in models:
            return asyncio.run(
                m._infer(
                    system_prompt="You are a helpful assistant summarizing an article for a person or other LLM wriitng an appeal. Be very concise.",
                    prompt=f"Given this query {query} summarize the following for {query}: {abstract_optional} {text_optional}.",
                )
            )
        return None

    def working(self) -> bool:
        """Return if we have candidates to route to. (TODO: Check they're alive)"""
        return len(self.all_models_by_cost) > 0


ml_router = MLRouter()

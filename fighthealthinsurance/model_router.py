import asyncio
from typing import List

from fighthealthinsurance.ml_models import *


class ModelRouter(object):
    """
    Tool to route our requests most cheapily.
    """

    models_by_name: dict[str, List[RemoteModelLike]] = {}
    internal_models_by_cost: List[RemoteModelLike] = []
    all_models_by_cost: List[RemoteModelLike] = []

    def __init__(self):
        print(f"Starting model 'router'")
        building_internal_models_by_cost = []
        building_all_models_by_cost = []
        building_models_by_name: dict[str, List[ModelDescription]] = {}
        for backend in candidate_model_backends:
            print(f"Considering {backend}")
            try:
                models = backend.models()
                print(f"{backend} gave us {models}")
                for m in models:
                    print(f"Adding {m} from {backend}")
                    if m.model is None:
                        m.model = backend(model=m.internal_name)
                        print(f"Built {m.model}")
                    if not m.model.external():
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
                    print(f"Added {m}")
            except Exception as e:
                print(f"Skipping {backend} due to {e} of {type(e)}")
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
        print(
            f"Built {self} with i:{self.internal_models_by_cost} a:{self.all_models_by_cost}"
        )

    def entity_extract_backends(self, use_external) -> list[RemoteModelLike]:
        """Backends for entity extraction."""
        if use_external:
            return self.all_models_by_cost
        else:
            return self.internal_models_by_cost

    def summarize(self, text: str, query: str, abstract: str) -> Optional[str]:
        models: list[RemoteModelLike] = []
        if "meta-llama/llama-3.1-70b-instruct" in self.models_by_name:
            models = self.models_by_name["meta-llama/llama-3.1-70b-instruct"]
        else:
            models = self.all_models_by_cost
        for m in models:
            return asyncio.run(
                m._infer(
                    system_prompt="You are a helpful assistant summarizing an article for a person or other LLM wriitng an appeal. Be very concise.",
                    prompt=f"Given this query {query} summarize the following for {query}: {abstract} -- {text}.",
                )
            )
        return None

    def working(self) -> bool:
        """Return if we have candidates to route to. (TODO: Check they're alive)"""
        return len(self.all_models_by_cost) > 0


model_router = ModelRouter()

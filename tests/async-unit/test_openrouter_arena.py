"""OpenRouterArena: the Appeal Arena's frontier models over OpenRouter,
synthetic-only like LunaRoute, and the arena's default forced models. No
network: the router is built against a fake environment, and nothing is
inferred.
"""

import os
from unittest.mock import patch

import pytest

from fighthealthinsurance.ml import ml_models
from fighthealthinsurance.ml.ml_models import (
    OpenRouter,
    OpenRouterArena,
    candidate_model_backends,
)
from fighthealthinsurance.ml.ml_router import MLRouter


@pytest.fixture
def router():
    """A router built with only OPENROUTER_API_KEY set (to a fake key)."""
    values = {"OPENROUTER_API_KEY": "sk-or-test"}
    with patch.object(ml_models, "get_env_variable", side_effect=values.get):
        yield MLRouter()


def test_arena_models_are_registered_by_name_for_the_arena(router):
    assert set(OpenRouterArena.ARENA_MODELS) <= set(
        router.chat_outside_models_by_name
    )


def test_arena_models_join_no_general_pool(router):
    pooled = {str(m) for m in router.all_models_by_cost}
    assert not any("openrouter-arena" in p for p in pooled), pooled


def test_arena_models_are_refused_to_chat_even_when_named(router):
    names = list(OpenRouterArena.ARENA_MODELS)
    assert router.chat_outside_models(names, fail_open=True) == []


def test_arena_instances_call_the_openrouter_model_id(router):
    for name, model_id in OpenRouterArena.ARENA_MODELS.items():
        assert router.chat_outside_models_by_name[name].model == model_id


def test_arena_requests_deny_data_collection_without_requiring_zdr():
    assert OpenRouterArena.provider_preferences() == {"data_collection": "deny"}


def test_general_openrouter_models_still_require_zdr():
    with patch.object(ml_models, "get_env_variable", return_value=None):
        assert OpenRouter.provider_preferences()["zdr"] is True


def test_no_arena_model_is_registered_without_a_key():
    with patch.object(ml_models, "get_env_variable", return_value=None):
        router = MLRouter()
    assert not set(OpenRouterArena.ARENA_MODELS) & set(
        router.chat_outside_models_by_name
    )


@pytest.mark.skipif(
    "CHOOSER_FORCE_MODEL" in os.environ,
    reason="CHOOSER_FORCE_MODEL in the environment replaces the default",
)
def test_every_default_forced_model_names_a_backend_model():
    """A typo in the default list would be logged once and silently skipped,
    so each name must be one a backend lists (configured or not)."""
    from fighthealthinsurance.settings import Base

    known = set()
    for backend in candidate_model_backends:
        known |= {m.name for m in backend.model_catalog()}
        known |= {m.name for m in backend.chat_models()}
    defaults = [n.strip() for n in Base.CHOOSER_FORCE_MODEL.split(",") if n.strip()]
    assert defaults == [
        "lunaroute/deepseek-4.1-flash",
        "openrouter-arena/anthropic/claude-fable-5.2",
        "openrouter-arena/openai/gpt-6.1-terra",
        "nvidia/NVIDIA-Nemotron-3.5-Super-VL",
    ]
    assert set(defaults) <= known, set(defaults) - known

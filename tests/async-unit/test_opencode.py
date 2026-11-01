"""OpenCode JEV and regular-model routing stays ordered and hermetic."""

import os
from unittest.mock import patch

import pytest
from django.test import override_settings

from fighthealthinsurance.ml import spend, typesafe
from fighthealthinsurance.ml.ml_models import DeepInfra, OpenCode
from fighthealthinsurance.ml.ml_router import MLRouter


class _Response:
    def __init__(self, status, model):
        self.status = status
        self._model = model

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return {"answers": {}, "model": self._model, "usage": {"input_tokens": 10}}

    async def text(self, **kwargs):
        return ""


class _Session:
    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self._calls.append((url, kwargs["json"]["model"]))
        return self._responses.pop(0)


def _sessions(responses, calls):
    return lambda **kwargs: _Session(responses, calls)


@pytest.mark.asyncio
async def test_base_jev_uses_opencode_before_typesafe_and_falls_through():
    calls = []
    responses = [_Response(429, ""), _Response(200, typesafe.DEFAULT_MODEL)]
    with (
        override_settings(
            OPENCODE_TOKEN="open-test-token",
            TYPESAFE_API_KEY="premium-test-token",
            TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone",
            TYPESAFE_MODEL=typesafe.DEFAULT_MODEL,
        ),
        patch.object(typesafe.spend, "allows", return_value=True),
        patch.object(typesafe.spend, "record"),
        patch.object(typesafe.llm_usage, "record_llm_usage"),
        patch.object(typesafe.aiohttp, "ClientSession", _sessions(responses, calls)),
    ):
        await typesafe.ask("state", {}, timeout_seconds=1)

    assert calls == [
        (typesafe.OPENCODE_API_URL, typesafe.OPENCODE_MODEL),
        ("https://typesafe.invalid/v1/systemone", typesafe.DEFAULT_MODEL),
    ]


@pytest.mark.asyncio
async def test_custom_jev_model_always_uses_premium():
    calls = []
    responses = [_Response(200, "jev-fhi-ft")]
    with (
        override_settings(
            OPENCODE_TOKEN="open-test-token",
            TYPESAFE_API_KEY="premium-test-token",
            TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone",
            TYPESAFE_MODEL="jev-fhi-ft",
        ),
        patch.object(typesafe.spend, "allows", return_value=True),
        patch.object(typesafe.spend, "record"),
        patch.object(typesafe.llm_usage, "record_llm_usage"),
        patch.object(typesafe.aiohttp, "ClientSession", _sessions(responses, calls)),
    ):
        await typesafe.ask("state", {}, timeout_seconds=1)

    assert calls == [("https://typesafe.invalid/v1/systemone", "jev-fhi-ft")]


def test_custom_jev_model_is_not_enabled_by_opencode_token_alone():
    with override_settings(
        OPENCODE_TOKEN="open-test-token",
        TYPESAFE_API_KEY=None,
        TYPESAFE_MODEL="jev-fhi-ft",
    ):
        assert not typesafe.configured()


def test_regular_flash_model_orders_free_opencode_before_deepinfra():
    env = {"OPENCODE_TOKEN": "open-test-token", "DEEPINFRA_API": "deep-test-token"}
    with patch.dict(os.environ, env, clear=False):
        router = MLRouter()

    backends = router.models_by_name[OpenCode.FLASH_NAME]
    assert [type(backend) for backend in backends] == [OpenCode, DeepInfra]
    assert [backend.model for backend in backends] == [
        OpenCode.FLASH_MODEL,
        OpenCode.FLASH_NAME,
    ]


def test_a_whitespace_only_token_is_rejected_like_a_missing_one():
    """config_status reports it as missing_credentials; the constructor must
    not accept what the status page calls missing."""
    with patch.dict(os.environ, {"OPENCODE_TOKEN": "   "}, clear=False):
        assert OpenCode.config_status()[0] == "missing_credentials"
        with pytest.raises(Exception, match="No token found for OpenCode"):
            OpenCode(OpenCode.FLASH_MODEL)


@pytest.mark.asyncio
async def test_the_usage_row_names_the_free_model_when_the_answer_names_none():
    calls = []
    responses = [_Response(200, "")]
    with (
        override_settings(OPENCODE_TOKEN="open-test-token"),
        patch.object(typesafe.spend, "record") as spend_record,
        patch.object(
            typesafe.llm_usage,
            "record_llm_usage",
            side_effect=lambda **kw: calls.append(kw),
        ),
        patch.object(typesafe.aiohttp, "ClientSession", _sessions(responses, [])),
    ):
        await typesafe.ask("state", {}, timeout_seconds=1)

    assert calls[-1]["model"] == f"opencode/{typesafe.OPENCODE_MODEL}"
    spend_record.assert_not_called()

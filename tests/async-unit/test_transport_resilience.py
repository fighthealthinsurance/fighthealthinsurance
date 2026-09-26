"""Transport behaviour the review found missing: the attempt deadline
reaching appeal-path calls, a primary HTTP error letting the backup answer,
dual-mode legs cancelled with their caller, and the Azure Claude transport
honouring cooldowns."""

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from fighthealthinsurance.ml import ml_models
from fighthealthinsurance.ml.ml_models import (
    RemoteAzureClaude,
    RemoteFullOpenLike,
    RemoteOpenLike,
)

LONG_REPLY = (
    "Dear Insurance Company, I am writing to appeal the denial of coverage for "
    "the requested procedure, which is medically necessary given the documented "
    "diagnosis and history. " * 3
)
GOOD_JSON = {
    "choices": [{"message": {"role": "assistant", "content": LONG_REPLY}}]
}
AZURE_CLAUDE_ENV = {
    "AZURE_ANTHROPIC_API_KEY": "test-key",
    "AZURE_ANTHROPIC_ENDPOINT": "https://res.services.ai.azure.com/anthropic",
}


class TestAttemptDeadlineReachesAppealCalls:
    def test_parallel_infer_carries_the_deadline_into_the_worker(self, monkeypatch):
        """A raw executor submit does not copy the context, so the clamp
        read in the worker saw no deadline."""
        model = RemoteFullOpenLike("http://x.example/v1", "tok", "m")
        seen = []

        def fake_blocking(**kwargs):
            seen.append(ml_models.ml_task_timeout("appeal"))
            return []

        monkeypatch.setattr(model, "_blocking_checked_infer", fake_blocking)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with ml_models.attempt_deadline(25.0):
                futures = model.parallel_infer(
                    prompt="p",
                    infer_type="full",
                    patient_context=None,
                    plan_context=None,
                    pubmed_context=None,
                    submit_executor=pool,
                )
                for f in futures:
                    f.result()
        assert seen
        assert all(t <= 25.0 for t in seen), seen

    @pytest.mark.asyncio
    async def test_checked_infer_passes_the_clamped_timeout(self):
        """The appeal-path calls passed no timeout, so the instance default
        (the full configured value) applied inside every attempt."""
        model = RemoteFullOpenLike("http://x.example/v1", "tok", "m")
        model._infer_no_context = AsyncMock(return_value=LONG_REPLY)  # type: ignore[method-assign]
        with ml_models.attempt_deadline(25.0):
            await model._checked_infer("p", None, None, "full", None, "sys", 0.5)
        kwargs = model._infer_no_context.call_args.kwargs
        assert 0 < kwargs["timeout"] <= 25.0, kwargs


class _RoutedPost:
    """ClientSession.post stand-in answering by URL prefix."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []
        self._current = None

    def __call__(self, url, *args, **kwargs):
        self.calls.append(str(url))
        self._current = next(
            r for prefix, r in self._responses.items() if str(url).startswith(prefix)
        )
        return self

    async def __aenter__(self):
        return self._current

    async def __aexit__(self, *exc):
        return False


class TestPrimaryHttpErrorTriesTheBackup:
    @pytest.mark.asyncio
    async def test_a_502_on_the_primary_lets_the_backup_answer(
        self, monkeypatch, make_fake_model_post
    ):
        """The HTTP error escaped the whole prompt loop, so the backup below
        it was never tried."""
        model = RemoteFullOpenLike(
            "http://primary.example/v1",
            "tok",
            "m",
            backup_api_base="http://backup.example/v1",
        )
        bad = make_fake_model_post(502, "upstream error")._response
        good = make_fake_model_post(200, "{}", json_data=GOOD_JSON)._response
        post = _RoutedPost(
            {"http://primary.example": bad, "http://backup.example": good}
        )
        monkeypatch.setattr(aiohttp.ClientSession, "post", post)

        result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is not None and result[0] == LONG_REPLY
        assert [c.split("/v1")[0] for c in post.calls] == [
            "http://primary.example",
            "http://backup.example",
        ]

    @pytest.mark.asyncio
    async def test_a_probe_still_sees_the_http_error_when_the_backup_fails_too(
        self, monkeypatch, make_fake_model_post
    ):
        model = RemoteFullOpenLike(
            "http://primary.example/v1",
            "tok",
            "m",
            backup_api_base="http://backup.example/v1",
        )
        bad = make_fake_model_post(502, "upstream error")._response
        post = _RoutedPost({"http://": bad})
        monkeypatch.setattr(aiohttp.ClientSession, "post", post)

        with pytest.raises(aiohttp.ClientResponseError) as excinfo:
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        assert excinfo.value.status == 502


class TestDualModeCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_the_call_cancels_both_legs(self, monkeypatch):
        """Cancelling the coroutine did not cancel the legs it created; both
        ran on to their own timeout."""
        model = RemoteFullOpenLike(
            "http://primary.example/v1",
            "tok",
            "m",
            backup_api_base="http://backup.example/v1",
            dual_mode=True,
        )
        legs = []

        async def slow_leg(self, *args, **kwargs):
            legs.append(asyncio.current_task())
            await asyncio.sleep(5)
            return (LONG_REPLY, None)

        monkeypatch.setattr(RemoteOpenLike, "_RemoteOpenLike__timeout_infer", slow_leg)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                model._infer(system_prompts=["sys"], prompt="hi"), timeout=0.1
            )
        await asyncio.sleep(0)

        assert len(legs) == 2
        assert all(leg.cancelled() for leg in legs), [leg.done() for leg in legs]


class TestAzureClaudeCooldowns:
    def _model(self):
        with patch.dict(os.environ, AZURE_CLAUDE_ENV):
            return RemoteAzureClaude(model="claude-opus-4-8")

    @pytest.mark.asyncio
    async def test_a_transport_failure_strikes_the_endpoint(self, monkeypatch):
        """The Messages transport bypassed the shared strike accounting, so a
        dead Foundry endpoint never entered a cooldown."""
        model = self._model()

        def refuse(*args, **kwargs):
            raise aiohttp.ClientConnectionError("connection refused")

        monkeypatch.setattr(aiohttp.ClientSession, "post", refuse)
        with patch.object(model, "_note_transport_failure") as note:
            result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is None
        note.assert_called_once()
        assert note.call_args.args[:2] == (model.api_base, "claude-opus-4-8")

    @pytest.mark.asyncio
    async def test_a_probe_does_not_strike(self, monkeypatch):
        model = self._model()

        def refuse(*args, **kwargs):
            raise aiohttp.ClientConnectionError("connection refused")

        monkeypatch.setattr(aiohttp.ClientSession, "post", refuse)
        with patch.object(model, "_note_transport_failure") as note:
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        note.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_endpoint_in_cooldown_is_not_called(self, monkeypatch):
        model = self._model()
        model._transport_cooldowns[(model.api_base, "claude-opus-4-8")] = (
            time.monotonic() + 100
        )

        def explode(*args, **kwargs):
            raise AssertionError("the endpoint was called")

        monkeypatch.setattr(aiohttp.ClientSession, "post", explode)
        assert await model._infer(system_prompts=["sys"], prompt="hi") is None

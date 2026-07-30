"""Per-task ML timeout configuration and bounded retry loops.

Covers:
- ``ml_task_timeout`` env overrides (and garbage-value fallback)
- ``sock_connect`` fail-fast: an unreachable host errors out in seconds
  instead of consuming the whole task budget
- the prof-pov/PA retry loop and the chat panda-retry loop are capped at 2
  extra attempts (previously 5 and 4, which could pin an executor thread for
  over half an hour of serial model calls)
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.ml.ml_models import (
    RemoteFullOpenLike,
    _env_float,
    ml_task_timeout,
)


class TestMlTaskTimeoutConfig:
    def test_defaults(self):
        assert ml_task_timeout("appeal") == 300.0
        assert ml_task_timeout("chat") == 90.0
        assert ml_task_timeout("entity") == 45.0
        assert ml_task_timeout("context") == 120.0
        assert ml_task_timeout("summarize") == 60.0

    def test_unknown_task_falls_back_to_appeal_budget(self):
        assert ml_task_timeout("nonsense") == ml_task_timeout("appeal")

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FHI_ML_TIMEOUT_CHAT", "17.5")
        assert ml_task_timeout("chat") == 17.5

    def test_garbage_env_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("FHI_ML_TIMEOUT_ENTITY", "not-a-number")
        assert ml_task_timeout("entity") == 45.0

    def test_env_float_empty_string_falls_back(self, monkeypatch):
        monkeypatch.setenv("FHI_ML_TIMEOUT", "")
        assert _env_float("FHI_ML_TIMEOUT", 300.0) == 300.0


class TestSockConnectFailFast:
    @pytest.mark.asyncio
    async def test_unreachable_host_fails_fast(self):
        """Port 1 on localhost refuses/black-holes connections; with
        sock_connect bounded the call must degrade to None in far less time
        than the task budget."""
        model = RemoteFullOpenLike("http://127.0.0.1:1/v1", "test-token", "test-model")
        start = time.monotonic()
        result = await model._infer_no_context(
            system_prompts=["You are a helpful assistant."],
            prompt="Hello",
            timeout=60.0,
        )
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 15.0, f"connect to dead host took {elapsed:.1f}s"


def _mk_model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://test.invalid/v1", "tok", "m")


class TestBoundedRetryLoops:
    @pytest.mark.asyncio
    async def test_prof_pov_loop_capped_at_two_retries(self):
        model = _mk_model()
        calls = []

        async def fake_infer_no_context(**kwargs):
            calls.append(kwargs)
            return "a perfectly reasonable draft appeal body"

        with (
            patch.object(model, "_infer_no_context", side_effect=fake_infer_no_context),
            patch.object(model, "check_is_ok", return_value=False),
            patch.object(model, "bad_result", return_value=False),
        ):
            result = await model._checked_infer(
                prompt="write an appeal",
                patient_context=None,
                plan_context=None,
                infer_type="full",
                pubmed_context=None,
                system_prompt="sys",
                temperature=0.7,
                prof_pov=True,
            )
        # 1 initial call + at most 2 prof-pov retries.
        assert len(calls) == 3, f"expected 3 calls, saw {len(calls)}"
        assert result  # the last okish result is still returned

    @pytest.mark.asyncio
    async def test_chat_panda_loop_capped_at_two_attempts(self):
        model = _mk_model()
        calls = []

        async def fake_infer(**kwargs):
            calls.append(kwargs)
            # Never contains the panda emoji and always "bad", forcing the
            # loop to keep retrying until its cap.
            return (None, None)

        with patch.object(model, "_infer", side_effect=fake_infer):
            answer, summary = await model.generate_chat_response(
                "Help me appeal my denial",
                previous_context_summary=None,
                history=[],
            )
        assert answer is None
        assert len(calls) == 2, f"expected 2 attempts, saw {len(calls)}"

    @pytest.mark.asyncio
    async def test_chat_calls_carry_chat_timeout(self):
        model = _mk_model()
        seen_timeouts = []

        async def fake_infer(**kwargs):
            seen_timeouts.append(kwargs.get("timeout"))
            return ("An answer 🐼 summary", None)

        with patch.object(model, "_infer", side_effect=fake_infer):
            await model.generate_chat_response("Hi there", history=[])
        assert seen_timeouts and seen_timeouts[0] == ml_task_timeout("chat")

    @pytest.mark.asyncio
    async def test_entity_calls_carry_entity_timeout(self):
        model = _mk_model()
        seen_timeouts = []

        async def fake_infer(**kwargs):
            seen_timeouts.append(kwargs.get("timeout"))
            return ("ACME Insurance", None)

        with patch.object(model, "_infer", side_effect=fake_infer):
            await model.get_entity("some denial text", "insurance_company")
        assert seen_timeouts and seen_timeouts[0] == ml_task_timeout("entity")

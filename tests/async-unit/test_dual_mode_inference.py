"""Tests for dual-mode inference's "first valid answer wins" contract.

Dual mode races the primary and backup backends. The winner must be returned
without awaiting the straggler: blocking on it would stall every dual-mode
call on the slower backend (a dead one adds its whole connect timeout) and
let its late result overwrite the fast one. When the first finisher's result
is invalid, the other task's result is still used.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike

# Far above any plausible test-box scheduling jitter, far below the
# straggler's sleep: regressions that re-await the straggler blow past it.
FAST_PATH_BUDGET_S = 5.0
STRAGGLER_SLEEP_S = 30.0


def _dual_model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike(
        "http://fast.example/v1",
        "test-token",
        "fast-model",
        backup_api_base="http://slow.example/v1",
        backup_model="slow-model",
        dual_mode=True,
    )


class TestDualModeFirstWins:
    @pytest.mark.asyncio
    async def test_fast_valid_result_returns_without_awaiting_straggler(self):
        model = _dual_model()

        async def fake_timeout_infer(*args, **kwargs):
            if kwargs.get("model") == "fast-model":
                return ("fast answer", [])
            await asyncio.sleep(STRAGGLER_SLEEP_S)
            return ("slow answer", [])

        with patch.object(
            model, "_RemoteOpenLike__timeout_infer", new=fake_timeout_infer
        ):
            start = time.monotonic()
            result = await model._infer(system_prompts=["sys"], prompt="hi")
            elapsed = time.monotonic() - start

        assert result is not None
        assert result[0] == "fast answer"
        assert elapsed < FAST_PATH_BUDGET_S
        # Let the cancelled straggler finalize so the loop closes clean.
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_invalid_first_result_falls_back_to_other_task(self):
        model = _dual_model()

        async def fake_timeout_infer(*args, **kwargs):
            if kwargs.get("model") == "fast-model":
                return None
            await asyncio.sleep(0.05)
            return ("slow answer", [])

        with patch.object(
            model, "_RemoteOpenLike__timeout_infer", new=fake_timeout_infer
        ):
            result = await model._infer(system_prompts=["sys"], prompt="hi")

        assert result is not None
        assert result[0] == "slow answer"

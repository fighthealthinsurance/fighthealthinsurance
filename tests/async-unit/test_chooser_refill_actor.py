"""ChooserRefillActor's run guard and health check, without Ray."""

from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.base_actor_ref import RUN_ALREADY_STARTED
from fighthealthinsurance.chooser_refill_actor import (
    UNHEALTHY_AFTER_FAILURES,
    ChooserRefillActor,
)

# The class under the Ray decorator (see test_imr_refresh_actor.py).
_Klass = getattr(ChooserRefillActor, "__ray_metadata__", None)
_Underlying = _Klass.modified_class if _Klass else ChooserRefillActor


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = info

    def opt(self, **k):
        return self


def _actor(running=False, failures=0):
    # __new__ bypasses __init__ (Django bootstrap plus a sleep).
    actor = _Underlying.__new__(_Underlying)
    actor._logger = _Logger()
    actor.running = running
    actor._consecutive_failures = failures
    return actor


@pytest.mark.asyncio
async def test_a_second_run_returns_at_once_instead_of_looping():
    """Async actors run calls concurrently; a second run() used to become a
    second refill loop inside the same actor."""
    actor = _actor(running=True)
    with patch(
        "fighthealthinsurance.chooser_tasks.check_and_refill_task_pool",
        new=AsyncMock(side_effect=AssertionError("the loop ran")),
    ):
        assert await actor.run() == RUN_ALREADY_STARTED


@pytest.mark.asyncio
async def test_health_check_needs_running_and_recent_success():
    assert await _actor(running=True).health_check() is True
    assert await _actor(running=False).health_check() is False
    assert (
        await _actor(running=True, failures=UNHEALTHY_AFTER_FAILURES).health_check()
        is False
    )


@pytest.mark.asyncio
async def test_a_loop_failing_every_tick_stops_reporting_healthy():
    """The flag alone was true from the first run() onward, so a loop whose
    every tick raised reported healthy forever."""
    actor = _actor()
    ticks = []

    async def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) >= UNHEALTHY_AFTER_FAILURES:
            actor.running = False

    with patch(
        "fighthealthinsurance.chooser_tasks.check_and_refill_task_pool",
        new=AsyncMock(side_effect=RuntimeError("db credentials rotated")),
    ), patch("fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep):
        await actor.run()

    assert actor._consecutive_failures == UNHEALTHY_AFTER_FAILURES
    # running is False now, so the check is False either way; the counter is
    # what the next run() would report against.
    actor.running = True
    assert await actor.health_check() is False


@pytest.mark.asyncio
async def test_a_successful_tick_resets_the_failure_count():
    actor = _actor(failures=2)

    async def fake_sleep(seconds):
        actor.running = False

    with patch(
        "fighthealthinsurance.chooser_tasks.check_and_refill_task_pool",
        new=AsyncMock(return_value=None),
    ), patch("fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep):
        await actor.run()

    assert actor._consecutive_failures == 0

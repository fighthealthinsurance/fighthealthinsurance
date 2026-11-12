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
    ), patch(
        "fighthealthinsurance.condition_rankings.refresh_if_due", new=AsyncMock()
    ), patch(
        "fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep
    ):
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
        new=AsyncMock(return_value=True),
    ), patch(
        "fighthealthinsurance.condition_rankings.refresh_if_due", new=AsyncMock()
    ), patch(
        "fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep
    ):
        await actor.run()

    assert actor._consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_slow_rankings_refresh_is_given_up_on():
    """With NCBI black-holed a refresh could hold the loop for ten minutes;
    the actor waits CONDITION_RANKING_REFRESH_TIMEOUT and moves on."""
    import asyncio

    actor = _actor(failures=0)
    ticks = []
    # The patch below replaces asyncio.sleep itself (the actor module's
    # asyncio is the asyncio module), so the slow refresh keeps the real one.
    real_sleep = asyncio.sleep

    async def slow_refresh():
        await real_sleep(5)

    async def fake_sleep(seconds):
        ticks.append(seconds)
        actor.running = False

    with patch(
        "fighthealthinsurance.chooser_tasks.check_and_refill_task_pool",
        new=AsyncMock(return_value=True),
    ), patch(
        "fighthealthinsurance.condition_rankings.refresh_if_due", new=slow_refresh
    ), patch(
        "fighthealthinsurance.condition_rankings.CONDITION_RANKING_REFRESH_TIMEOUT",
        0.05,
    ), patch(
        "fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep
    ):
        await actor.run()

    assert ticks == [300]
    assert actor._consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_failing_rankings_refresh_is_not_a_failed_tick():
    """The loop refreshes the condition rankings after each refill; a
    vocabulary service being down must not get the actor replaced, since
    the pool it exists for was refilled."""
    actor = _actor(failures=2)
    refresh = AsyncMock(side_effect=RuntimeError("ncbi down"))

    async def fake_sleep(seconds):
        actor.running = False

    with patch(
        "fighthealthinsurance.chooser_tasks.check_and_refill_task_pool",
        new=AsyncMock(return_value=True),
    ), patch(
        "fighthealthinsurance.condition_rankings.refresh_if_due", new=refresh
    ), patch(
        "fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep
    ):
        await actor.run()

    assert refresh.await_count == 1
    assert actor._consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_refill_that_produces_nothing_is_a_failed_tick():
    """The refill catches generation errors per task and marks the task
    DISABLED, so a tick where every model failed returned normally, reset the
    count, and the actor reported healthy while the pool drained."""
    actor = _actor(failures=UNHEALTHY_AFTER_FAILURES - 1)

    async def fake_sleep(seconds):
        actor.running = False

    with patch(
        "fighthealthinsurance.chooser_tasks.check_and_refill_task_pool",
        new=AsyncMock(return_value=False),
    ), patch(
        "fighthealthinsurance.condition_rankings.refresh_if_due", new=AsyncMock()
    ), patch(
        "fighthealthinsurance.chooser_refill_actor.asyncio.sleep", fake_sleep
    ):
        await actor.run()

    assert actor._consecutive_failures == UNHEALTHY_AFTER_FAILURES

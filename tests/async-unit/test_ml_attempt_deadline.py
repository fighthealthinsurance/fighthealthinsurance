"""Provider transport timeouts clamped to the attempt budget (enable gate 3).

The appeal task timeout defaults to 300s while the journey's generation
budget is 240s, so one provider call could outlive the whole attempt and keep
spending after Temporal moved on -- three attempts of that per workflow.
start_to_close does not kill the thread holding the socket, so the call must
not be allowed to outlive the budget in the first place.
"""

import asyncio
import time

import pytest
from asgiref.sync import sync_to_async

from fighthealthinsurance.ml import ml_models


def test_unclamped_outside_an_attempt():
    """Interactive and chat paths must be untouched."""
    assert ml_models.remaining_attempt_budget() is None
    assert ml_models.ml_task_timeout("appeal") == 300.0
    assert ml_models.ml_task_timeout("chat") == 90.0


def test_clamped_to_whats_left_of_the_budget():
    with ml_models.attempt_deadline(30.0):
        t = ml_models.ml_task_timeout("appeal")
    # 300s configured, 30s left -> 30s.
    assert 29.0 < t <= 30.0, t


def test_configured_value_wins_when_it_is_the_tighter_bound():
    """The clamp is a ceiling, never a floor: a short task timeout is not
    stretched to fill the budget."""
    with ml_models.attempt_deadline(600.0):
        assert ml_models.ml_task_timeout("entity") == 45.0


def test_never_returns_a_zero_or_negative_timeout():
    """requests treats 0 as fail-immediately and None as wait-forever, so a
    budget that has already run out must still yield a small positive value
    rather than either of those."""
    with ml_models.attempt_deadline(0.0):
        t = ml_models.ml_task_timeout("appeal")
    assert t == ml_models.MIN_TASK_TIMEOUT_SECONDS
    assert t > 0


def test_the_floor_never_lengthens_a_short_configured_timeout(monkeypatch):
    """The floor guards against handing a client 0 or a negative value; it is
    not a licence to make a deliberately short timeout longer. max(1.0, ...)
    turned a configured 0.5s into 1.0s (external review)."""
    monkeypatch.setenv("FHI_ML_TIMEOUT_ENTITY", "0.5")
    with ml_models.attempt_deadline(0.0):
        t = ml_models.ml_task_timeout("entity")
    assert t == 0.5, t
    assert t > 0


def test_nested_blocks_keep_the_tighter_deadline():
    """An inner stage may not award itself more time than the attempt has."""
    with ml_models.attempt_deadline(20.0):
        with ml_models.attempt_deadline(600.0):
            assert ml_models.ml_task_timeout("appeal") <= 20.0
        assert ml_models.ml_task_timeout("appeal") <= 20.0
    assert ml_models.remaining_attempt_budget() is None


def test_the_deadline_is_restored_even_when_the_block_raises():
    with pytest.raises(RuntimeError):
        with ml_models.attempt_deadline(10.0):
            raise RuntimeError("boom")
    assert ml_models.remaining_attempt_budget() is None


@pytest.mark.asyncio
async def test_the_clamp_reaches_a_threaded_provider_call():
    """The load-bearing assumption: provider calls run in worker THREADS via
    asgiref, and a ContextVar set on the event loop must reach them. If
    asgiref did not copy the context, the clamp would silently do nothing
    where it matters most."""

    def in_a_worker_thread() -> float:
        # Exactly what a blocking backend does before opening its socket.
        return ml_models.ml_task_timeout("appeal")

    with ml_models.attempt_deadline(25.0):
        seen = await sync_to_async(in_a_worker_thread, thread_sensitive=False)()
    assert 24.0 < seen <= 25.0, seen


@pytest.mark.asyncio
async def test_concurrent_attempts_do_not_see_each_others_budget():
    """A ContextVar, not a global: two generations on one worker must not
    clamp each other."""

    async def run(budget: float, hold: float) -> float:
        with ml_models.attempt_deadline(budget):
            await asyncio.sleep(hold)
            return ml_models.ml_task_timeout("appeal")

    tight, loose = await asyncio.gather(run(5.0, 0.05), run(120.0, 0.05))
    assert tight <= 5.0, tight
    assert 100.0 < loose <= 120.0, loose

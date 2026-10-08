"""execute_critical_optional_fireandforget stops its tasks when its consumer does."""

import asyncio

import pytest

from fighthealthinsurance.utils import execute_critical_optional_fireandforget


@pytest.mark.asyncio
async def test_a_consumer_timeout_cancels_the_running_tasks():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            async for _ in execute_critical_optional_fireandforget(
                required=[slow()], optional=[], timeout=30
            ):
                pass
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    assert started.is_set()

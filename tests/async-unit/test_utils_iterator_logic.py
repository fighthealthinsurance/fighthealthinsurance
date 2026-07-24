import pytest
import asyncio
import time
from typing import AsyncIterator
from fighthealthinsurance.utils import (
    interleave_iterator_for_keep_alive,
    keepalive_frames,
    sync_iterator_to_async,
)


async def async_generator(items, delay: float = 0.1) -> AsyncIterator[str]:
    """Test helper: Async generator yielding items with delay."""
    for item in items:
        await asyncio.sleep(delay)
        yield item


class SlowAsyncIterator:
    """Class-based async iterator that doesn't have the reentrancy restriction
    of async generators. This allows overlapping __anext__() calls which is
    what interleave_iterator_for_keep_alive does when timeouts occur."""

    def __init__(self, items, delay: float = 0.1):
        self.items = list(items)
        self.index = 0
        self.delay = delay

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.index]
        self.index += 1
        await asyncio.sleep(self.delay)
        return item


class TestInterleaveIterator:
    @pytest.mark.asyncio
    async def test_interleave_iterator_for_keep_alive_basic(self):
        """Test interleaving behavior of interleave_iterator_for_keep_alive.

        The implementation yields newlines (not empty strings) as keep-alive signals.
        Pattern per item (no timeouts): "\\n" before, item, "\\n" after
        Plus initial "\\n" and final "\\n" when iterator exhausts.
        """
        items = ["data1", "data2", "data3"]
        # Pattern: \n, [\n, data, \n]..., \n (final before StopAsyncIteration)
        expected_output = ["\n", "\n", "data1", "\n", "\n", "data2", "\n", "\n", "data3", "\n", "\n"]
        async_iter = async_generator(items)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter)
        result = [item async for item in interleaved_iter]
        assert result == expected_output

    @pytest.mark.asyncio
    async def test_interleave_iterator_for_keep_alive_slow(self):
        """Test that slow items cause extra keep-alive newlines to be emitted.

        Uses SlowAsyncIterator (class-based) with delay > timeout so that
        timeouts fire and keepalive newlines are emitted while waiting.
        All items should still be delivered (no item loss).
        """
        items = [f"data{i}" for i in range(3)]
        async_iter = SlowAsyncIterator(items, delay=1.5)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter, timeout=1)
        result = [item async for item in interleaved_iter]

        newline_count = result.count("\n")
        assert newline_count > 0, "Should have keep-alive newlines"

        data_items = [r for r in result if r != "\n"]

        # All items must be delivered (no item loss)
        assert data_items == items, (
            f"Expected all items {items}, got {data_items}"
        )

        # Extra newlines from timeouts prove the keep-alive path was exercised
        assert newline_count > 11, (
            f"Expected extra timeout-induced keep-alive newlines, got only {newline_count}"
        )

    @pytest.mark.asyncio
    async def test_interleave_with_async_generator(self):
        """interleave_iterator_for_keep_alive works with async generators
        (not just class-based iterators) when timeouts fire.

        This was previously broken: the old shield pattern called __anext__()
        on a still-running async generator, causing RuntimeError.
        """
        items = ["item1", "item2"]
        async_iter = async_generator(items, delay=1.5)
        interleaved = interleave_iterator_for_keep_alive(async_iter, timeout=1)

        result = []
        async for item in interleaved:
            result.append(item)

        data_items = [r for r in result if r != "\n"]
        assert data_items == items, (
            f"Expected {items}, got {data_items}"
        )


class BlockingSyncIterator:
    """Synchronous iterator that blocks (via time.sleep) on each next() call,
    simulating concurrent.futures.as_completed() behavior."""

    def __init__(self, items, delay: float = 0.5):
        self.items = list(items)
        self.index = 0
        self.delay = delay

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= len(self.items):
            raise StopIteration
        time.sleep(self.delay)
        item = self.items[self.index]
        self.index += 1
        return item


class TestSyncIteratorToAsync:
    @pytest.mark.asyncio
    async def test_basic_conversion(self):
        """All items from a sync iterator are yielded in order."""
        items = ["a", "b", "c"]
        result = [item async for item in sync_iterator_to_async(iter(items))]
        assert result == items

    @pytest.mark.asyncio
    async def test_empty_iterator(self):
        """Empty sync iterator produces no items."""
        result = [item async for item in sync_iterator_to_async(iter([]))]
        assert result == []

    @pytest.mark.asyncio
    async def test_none_items_preserved(self):
        """None items pass through (filtering is a separate concern)."""
        items = ["a", None, "b"]
        result = [item async for item in sync_iterator_to_async(iter(items))]
        assert result == items

    @pytest.mark.asyncio
    async def test_blocking_iterator_does_not_block_event_loop(self):
        """A blocking sync iterator must not prevent concurrent async work.

        This is the core regression test for the appeal streaming bug:
        if next() blocked the event loop, the concurrent task could not run.
        Uses an asyncio.Event to prove concurrent_work() finishes *while*
        consume() is still blocked waiting for iterator items.
        """
        blocking_iter = BlockingSyncIterator(["x", "y"], delay=0.5)

        concurrent_done = asyncio.Event()
        consume_done = asyncio.Event()

        async def concurrent_work():
            await asyncio.sleep(0.1)
            assert not consume_done.is_set(), (
                "concurrent_work must finish while consume is still running"
            )
            concurrent_done.set()

        async def consume():
            results = []
            async for item in sync_iterator_to_async(blocking_iter):
                results.append(item)
            consume_done.set()
            return results

        results, _ = await asyncio.gather(consume(), concurrent_work())
        assert results == ["x", "y"]
        assert concurrent_done.is_set(), (
            "Concurrent async task should have run while iterator was blocking"
        )

    @pytest.mark.asyncio
    async def test_keepalive_emitted_during_blocking_iteration(self):
        """End-to-end: keep-alive newlines are emitted even when the
        underlying sync iterator blocks, verifying the full pipeline."""
        blocking_iter = BlockingSyncIterator(["appeal1"], delay=2.0)
        async_iter = sync_iterator_to_async(blocking_iter)
        interleaved = interleave_iterator_for_keep_alive(async_iter, timeout=1)

        result = []
        async for item in interleaved:
            result.append(item)

        # With 1 item and no timeouts, the baseline pattern produces 4 newlines
        # (initial + before-item + after-item + final). With a 2s blocking delay
        # and 1s timeout, at least one extra keep-alive newline must fire.
        newline_count = result.count("\n")
        assert newline_count >= 5, (
            f"Expected keep-alive newlines beyond baseline 4, got {newline_count}"
        )
        # The actual data item must appear
        data_items = [r for r in result if r != "\n"]
        assert "appeal1" in data_items


class TestKeepaliveFrames:
    """keepalive_frames wraps a blocking awaitable and emits heartbeats while
    it is pending, WITHOUT ever cancelling it. This is the primary fix for the
    silent-generation window during make_appeals."""

    @pytest.mark.asyncio
    async def test_no_heartbeats_when_task_completes_fast(self):
        """A task that finishes within the first interval emits no heartbeats
        and is left completed (not cancelled)."""

        async def quick():
            return "done"

        task = asyncio.ensure_future(quick())
        frames = [f async for f in keepalive_frames(task, interval=0.2)]
        assert frames == []
        assert not task.cancelled()
        assert await task == "done"

    @pytest.mark.asyncio
    async def test_heartbeats_emitted_while_pending(self):
        """A task slower than the interval yields periodic heartbeats and is
        still readable afterwards (the helper must not cancel it)."""

        async def slow():
            await asyncio.sleep(0.5)
            return "slow-done"

        task = asyncio.ensure_future(slow())
        frames = [
            f
            async for f in keepalive_frames(
                task, interval=0.1, make_heartbeat=lambda elapsed: "hb"
            )
        ]
        # ~0.5s / 0.1s -> several heartbeats (loose bound to avoid flakiness).
        assert len(frames) >= 3
        assert all(f == "hb" for f in frames)
        assert not task.cancelled()
        # Critically, the task ran to completion in the background.
        assert await task == "slow-done"

    @pytest.mark.asyncio
    async def test_overall_timeout_returns_with_task_still_pending(self):
        """When overall_timeout elapses first, the generator returns while the
        task is still running and, crucially, NOT cancelled by the helper."""

        async def very_slow():
            await asyncio.sleep(5)
            return "never"

        task = asyncio.ensure_future(very_slow())
        frames = [
            f
            async for f in keepalive_frames(
                task, interval=0.1, overall_timeout=0.3, make_heartbeat=lambda e: "hb"
            )
        ]
        assert len(frames) >= 2
        assert not task.done()
        assert not task.cancelled()
        # Caller owns cleanup.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_task_exception_is_not_swallowed(self):
        """If the wrapped task raises, the generator returns without
        propagating and the caller still observes the exception via await."""

        async def boom():
            await asyncio.sleep(0.05)
            raise ValueError("boom")

        task = asyncio.ensure_future(boom())
        frames = [f async for f in keepalive_frames(task, interval=1.0)]
        assert frames == []
        assert not task.cancelled()
        with pytest.raises(ValueError, match="boom"):
            await task

    @pytest.mark.asyncio
    async def test_make_heartbeat_receives_increasing_elapsed(self):
        """The heartbeat callback receives a monotonically increasing elapsed
        time so callers can render progress."""

        async def slow():
            await asyncio.sleep(0.35)
            return "x"

        task = asyncio.ensure_future(slow())
        elapseds: list[float] = []

        def hb(elapsed: float) -> str:
            elapseds.append(elapsed)
            return "\n"

        async for _ in keepalive_frames(task, interval=0.1, make_heartbeat=hb):
            pass

        assert len(elapseds) >= 2
        assert elapseds == sorted(elapseds)
        assert elapseds[-1] >= 0.1


class TestFullAppealStreamingPipeline:
    @pytest.mark.asyncio
    async def test_pipeline_with_amap_chain(self):
        """End-to-end: blocking sync iter -> sync_iterator_to_async ->
        a.map chain (async generators) -> interleave_iterator_for_keep_alive.

        This simulates the real appeal streaming pipeline and verifies:
        1. Keep-alive newlines are emitted during blocking waits
        2. All data items arrive (no items lost)
        3. No 'async generator already running' errors
        """
        import asyncstdlib as a

        blocking_iter = BlockingSyncIterator(["appeal1"], delay=2.0)
        async_iter = sync_iterator_to_async(blocking_iter)

        # a.map chain returns async generators (same as real code)
        async def wrap_dict(text):
            return {"content": text}

        async def to_json(d):
            return str(d)

        mapped1 = a.map(wrap_dict, async_iter)
        mapped2 = a.map(to_json, mapped1)

        # No QueuedAsyncIterator needed - the fixed interleave handles
        # async generators directly via Task + fresh shield pattern
        interleaved = interleave_iterator_for_keep_alive(mapped2, timeout=1)

        result = []
        async for item in interleaved:
            result.append(item)

        newline_count = result.count("\n")
        data_items = [r for r in result if r != "\n"]

        # Baseline for 1 item with no timeouts is 4 newlines; extra ones
        # prove keep-alive fired during the blocking wait.
        assert newline_count >= 5, (
            f"Expected keep-alive newlines beyond baseline 4, got {newline_count}"
        )
        # Appeal must arrive
        assert len(data_items) >= 1, (
            f"Expected at least 1 data item, got {len(data_items)}: {data_items}"
        )

    @pytest.mark.asyncio
    async def test_pipeline_multiple_items_no_loss(self):
        """Multiple items through the full pipeline - none should be lost."""
        import asyncstdlib as a

        blocking_iter = BlockingSyncIterator(["a1", "a2", "a3"], delay=0.3)
        async_iter = sync_iterator_to_async(blocking_iter)

        async def identity(x):
            return x

        mapped = a.map(identity, async_iter)
        interleaved = interleave_iterator_for_keep_alive(mapped, timeout=1)

        result = []
        async for item in interleaved:
            result.append(item)

        data_items = [r for r in result if r != "\n"]
        assert data_items == ["a1", "a2", "a3"], (
            f"Expected all items, got {data_items}"
        )

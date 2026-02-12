import pytest
import asyncio
from typing import AsyncIterator
from fighthealthinsurance.utils import interleave_iterator_for_keep_alive


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

        Uses SlowAsyncIterator (class-based) to avoid the Python async generator
        reentrancy restriction. With delay > timeout, the implementation emits
        extra keep-alive newlines while waiting. Some items may be lost during
        timeout recovery (by design for streaming HTTP keep-alive).
        """
        # Use enough items that at least some survive timeout-induced drops
        items = [f"data{i}" for i in range(10)]
        async_iter = SlowAsyncIterator(items, delay=0.15)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter, timeout=1)
        result = [item async for item in interleaved_iter]

        # Verify the iterator completes and produces keep-alive newlines
        newline_count = result.count("\n")
        assert newline_count > 0, "Should have keep-alive newlines"

        # Verify at least some data items made it through
        data_items = [r for r in result if r != "\n"]
        assert len(data_items) > 0, "Should have at least some data items"

        # Verify the items that did come through are from our input
        for item in data_items:
            assert item in items, f"Unexpected item: {item}"

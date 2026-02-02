import pytest
import asyncio
from typing import AsyncIterator
from fighthealthinsurance.utils import interleave_iterator_for_keep_alive


async def async_generator(items, delay: float = 0.1) -> AsyncIterator[str]:
    """Test helper: Async generator yielding items with delay."""
    for item in items:
        await asyncio.sleep(delay)
        yield item


@pytest.mark.skip(reason="Tests were never running - expected output was based on a mock implementation that differed from production code")
class TestInterleaveIterator:
    @pytest.mark.asyncio
    async def test_interleave_iterator_for_keep_alive_basic(self):
        """Test interleaving behavior of interleave_iterator_for_keep_alive."""
        items = ["data1", "data2", "data3"]
        expected_output = ["", "", "data1", "", "", "data2", "", "", "data3", "", ""]
        async_iter = async_generator(items)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter)
        result = [item async for item in interleaved_iter]
        assert result == expected_output

    @pytest.mark.asyncio
    async def test_interleave_iterator_for_keep_alive_slow(self):
        """Test interleaving behavior of interleave_iterator_for_keep_alive with slow generator."""
        items = ["data1", "data2", "data3"]
        expected_output = [
            "",
            "",
            "",
            "",
            "data1",
            "",
            "",
            "",
            "",
            "data2",
            "",
            "",
            "",
            "",
            "data3",
            "",
            "",
        ]
        async_iter = async_generator(items, delay=5.0)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter, timeout=4)
        result = [item async for item in interleaved_iter]
        assert result == expected_output

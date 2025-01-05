import pytest
import unittest
import asyncio
from typing import AsyncIterator, Iterator
from fighthealthinsurance.utils import *


async def async_generator(items, delay: float = 0.1) -> AsyncIterator[str]:
    """Test helper: Async generator yielding items with delay."""
    for item in items:
        await asyncio.sleep(delay)
        yield item

class TestInterleaveIterator(unittest.TestCase):
    def setUp(self):
        try:
            self.loop = asyncio.get_running_loop()
        except:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

    @pytest.mark.asyncio
    async def test_interleave_iterator_for_keep_alive_basic(self):
        """Test interleaving behavior of interleave_iterator_for_keep_alive."""
        items = ["data1", "data2", "data3"]
        expected_output = ["", "", "data1", "", "", "data2", "", "", "data3", "", ""]
        async_iter = async_generator(items)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter)
        result = [item async for item in interleaved_iter]
        self.assertEqual(result, expected_output)

    @pytest.mark.asyncio
    async def test_interleave_iterator_for_keep_alive_slow(self):
        """Test interleaving behavior of interleave_iterator_for_keep_alive."""
        items = ["data1", "data2", "data3"]
        expected_output = ["", "", "", "", "data1",
                           "", "", "", "", "data2",
                           "", "", "", "", "data3",
                           "", ""]
        async_iter = async_generator(items, delay=5.0)
        interleaved_iter = interleave_iterator_for_keep_alive(async_iter, timeout=4)
        result = [item async for item in interleaved_iter]
        self.assertEqual(result, expected_output)


if __name__ == "__main__":
    unittest.main()

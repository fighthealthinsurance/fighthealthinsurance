import unittest
import asyncio
from typing import AsyncIterator, Iterator
from fighthealthinsurance.utils import *


async def async_generator(items) -> AsyncIterator[str]:
    """Test helper: Async generator yielding items with delay."""
    for item in items:
        await asyncio.sleep(0.1)
        yield item

class TestInterleaveIterator(unittest.TestCase):
    def setUp(self):
        try:
            self.loop = asyncio.get_running_loop()
        except:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

    def test_interleave_iterator_for_keep_alive(self):
        """Test interleaving behavior of interleave_iterator_for_keep_alive."""
        async def test_case():
            items = ["data1", "data2", "data3"]
            expected_output = ["", "", "data1", "", "", "data2", "", "", "data3", ""]
            async_iter = async_generator(items)
            interleaved_iter = interleave_iterator_for_keep_alive(async_iter)
            result = [item async for item in interleaved_iter]
            self.assertEqual(result, expected_output)

        self.loop.run_until_complete(test_case())

    def test_async_to_sync_iterator(self):
        """Test conversion of async iterator to sync iterator."""
        async def async_test_case():
            items = ["item1", "item2", "item3"]
            async_iter = async_generator(items)
            sync_iter = async_to_sync_iterator(async_iter)
            return sync_iter

        sync_iter = self.loop.run_until_complete(async_test_case())

        # Collect sync iterator output
        result = list(sync_iter)
        expected_output = ["item1", "item2", "item3"]
        self.assertEqual(result, expected_output)

    def test_combined_behavior(self):
        """Test interleave_iterator_for_keep_alive with async_to_sync_iterator."""
        async def test_case():
            items = ["a", "b", "c"]
            interleaved_async_iter = interleave_iterator_for_keep_alive(async_generator(items))
            sync_iter = async_to_sync_iterator(interleaved_async_iter)
            return list(sync_iter)

        result = self.loop.run_until_complete(test_case())
        expected_output = ["", "", "a", "", "", "b", "", "", "c", ""]
        self.assertEqual(result, expected_output)

if __name__ == "__main__":
    unittest.main()

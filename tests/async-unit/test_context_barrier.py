"""Tests for the bounded-wait cache barrier."""

import asyncio
import json
import unittest
from unittest.mock import patch

from fighthealthinsurance.context_barrier import _is_populated, with_warm_cache


class TestIsPopulated(unittest.TestCase):
    def test_none(self):
        self.assertFalse(_is_populated(None))

    def test_empty_string(self):
        self.assertFalse(_is_populated(""))

    def test_nonempty_string(self):
        self.assertTrue(_is_populated("hello"))

    def test_empty_list(self):
        self.assertFalse(_is_populated([]))

    def test_nonempty_list(self):
        self.assertTrue(_is_populated(["x"]))

    def test_empty_dict(self):
        self.assertFalse(_is_populated({}))


class TestWithWarmCache(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_cache_hit(self):
        """If the cache is already populated, return it without polling."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        fallback_called = False

        async def fallback():
            nonlocal fallback_called
            fallback_called = True
            return "fallback"

        with patch(
            "fighthealthinsurance.context_barrier._read_field",
            return_value="cached_value",
        ):
            result = await with_warm_cache(
                denial_id=1,
                field_name="pubmed_context",
                barrier_timeout=10,
                poll_interval=0.5,
                status_queue=queue,
                substep="pubmed",
                ready_msg="ready",
                fallback_factory=fallback,
            )
        self.assertEqual(result, "cached_value")
        self.assertFalse(fallback_called)
        # One status frame emitted
        frame = json.loads(queue.get_nowait())
        self.assertEqual(frame["type"], "status")
        self.assertEqual(frame["phase"], "research")
        self.assertEqual(frame["substep"], "pubmed")
        self.assertEqual(frame["state"], "done")
        self.assertEqual(frame["message"], "ready")

    async def test_cache_populates_mid_wait(self):
        """Cache appears during the polling window — return it before fallback."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        call_count = 0

        async def read(denial_id, field_name):
            nonlocal call_count
            call_count += 1
            # First call (the upfront check) misses; second poll hits.
            if call_count <= 1:
                return None
            return "appeared"

        fallback_called = False

        async def fallback():
            nonlocal fallback_called
            fallback_called = True
            return "fallback"

        with patch(
            "fighthealthinsurance.context_barrier._read_field", side_effect=read
        ):
            result = await with_warm_cache(
                denial_id=1,
                field_name="pubmed_context",
                barrier_timeout=5,
                poll_interval=0.01,
                status_queue=queue,
                substep="pubmed",
                ready_msg="ready",
                fallback_factory=fallback,
            )
        self.assertEqual(result, "appeared")
        self.assertFalse(fallback_called)
        # Exactly one "done" frame
        self.assertEqual(queue.qsize(), 1)

    async def test_timeout_falls_through_to_fallback(self):
        """If the cache never populates, the fallback runs."""
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def fallback():
            return "from_fallback"

        with patch(
            "fighthealthinsurance.context_barrier._read_field", return_value=None
        ):
            result = await with_warm_cache(
                denial_id=1,
                field_name="pubmed_context",
                barrier_timeout=0.05,  # short timeout for speed
                poll_interval=0.01,
                status_queue=queue,
                substep="pubmed",
                ready_msg="ready",
                fallback_factory=fallback,
            )
        self.assertEqual(result, "from_fallback")
        # No status frame emitted — fallback owns its own frames
        self.assertEqual(queue.qsize(), 0)

    async def test_read_error_treated_as_cache_miss(self):
        """A DB error during cache polling falls through to fallback safely."""
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def read_failing(*args, **kwargs):
            raise RuntimeError("DB down")

        async def fallback():
            return "ok"

        with patch(
            "fighthealthinsurance.context_barrier._read_field",
            side_effect=read_failing,
        ):
            result = await with_warm_cache(
                denial_id=1,
                field_name="pubmed_context",
                barrier_timeout=0.05,
                poll_interval=0.01,
                status_queue=queue,
                substep="pubmed",
                ready_msg="ready",
                fallback_factory=fallback,
            )
        self.assertEqual(result, "ok")

    async def test_zero_timeout_still_checks_cache_once(self):
        """A barrier_timeout of 0 still does the initial cache check."""
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def fallback():
            return "fallback"

        with patch(
            "fighthealthinsurance.context_barrier._read_field",
            return_value="cached",
        ):
            result = await with_warm_cache(
                denial_id=1,
                field_name="pubmed_context",
                barrier_timeout=0,
                poll_interval=0.5,
                status_queue=queue,
                substep="pubmed",
                ready_msg="ready",
                fallback_factory=fallback,
            )
        self.assertEqual(result, "cached")

    async def test_no_status_queue_still_works(self):
        """Caller may omit the status queue; barrier returns silently."""
        with patch(
            "fighthealthinsurance.context_barrier._read_field",
            return_value="cached",
        ):
            result = await with_warm_cache(
                denial_id=1,
                field_name="pubmed_context",
                barrier_timeout=1,
                poll_interval=0.5,
                status_queue=None,
                substep="pubmed",
                ready_msg="ready",
                fallback_factory=lambda: asyncio.sleep(0, result="nope"),
            )
        self.assertEqual(result, "cached")


if __name__ == "__main__":
    unittest.main()

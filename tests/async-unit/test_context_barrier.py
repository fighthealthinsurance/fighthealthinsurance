"""Tests for the bounded-wait cache barrier."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fighthealthinsurance.context_barrier import (
    _is_populated,
    wait_for_warm_cache,
    warm_then_fetch,
)


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


def _fake_denial():
    """A denial whose arefresh_from_db is an async no-op spy."""
    d = SimpleNamespace(denial_id=1)
    d.arefresh_from_db = AsyncMock()
    return d


class TestWaitForWarmCache(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_readiness_refreshes_and_returns_true(self):
        denial = _fake_denial()
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            AsyncMock(return_value=True),
        ):
            ready = await wait_for_warm_cache(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=10,
                poll_interval=0.01,
            )
        self.assertTrue(ready)
        denial.arefresh_from_db.assert_awaited_once_with(fields=["pubmed_context"])

    async def test_readiness_appears_mid_wait(self):
        denial = _fake_denial()
        calls = {"n": 0}

        async def _poll(denial_id, fields):
            calls["n"] += 1
            return calls["n"] >= 2  # miss first, hit second

        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            side_effect=_poll,
        ):
            ready = await wait_for_warm_cache(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=5,
                poll_interval=0.01,
            )
        self.assertTrue(ready)
        denial.arefresh_from_db.assert_awaited_once()

    async def test_timeout_returns_false_no_refresh(self):
        denial = _fake_denial()
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            AsyncMock(return_value=False),
        ):
            ready = await wait_for_warm_cache(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=0.05,
                poll_interval=0.01,
            )
        self.assertFalse(ready)
        denial.arefresh_from_db.assert_not_awaited()

    async def test_zero_timeout_checks_once_and_does_not_poll(self):
        """timeout=0 (test config) must do exactly one readiness check and
        return immediately instead of polling a never-populated row."""
        denial = _fake_denial()
        probe = AsyncMock(return_value=False)
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            probe,
        ):
            ready = await wait_for_warm_cache(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=0,
                poll_interval=1.0,
            )
        self.assertFalse(ready)
        self.assertEqual(probe.await_count, 1)
        denial.arefresh_from_db.assert_not_awaited()

    async def test_db_error_degrades_to_not_ready(self):
        denial = _fake_denial()
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            ready = await wait_for_warm_cache(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=0.05,
                poll_interval=0.01,
            )
        self.assertFalse(ready)
        denial.arefresh_from_db.assert_not_awaited()

    async def test_refresh_error_is_swallowed(self):
        denial = _fake_denial()
        denial.arefresh_from_db = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            AsyncMock(return_value=True),
        ):
            # Must not raise even though refresh fails.
            ready = await wait_for_warm_cache(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=1,
                poll_interval=0.01,
            )
        self.assertTrue(ready)


class TestWarmThenFetch(unittest.IsolatedAsyncioTestCase):
    async def test_runs_fetch_after_readiness(self):
        denial = _fake_denial()
        fetch = AsyncMock(return_value="ctx")
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            AsyncMock(return_value=True),
        ):
            result = await warm_then_fetch(
                denial,
                readiness_fields=["pubmed_context"],
                refresh_fields=["pubmed_context"],
                barrier_timeout=1,
                fetch=lambda: fetch(),
                poll_interval=0.01,
            )
        self.assertEqual(result, "ctx")
        fetch.assert_awaited_once()
        denial.arefresh_from_db.assert_awaited_once()

    async def test_runs_fetch_on_timeout(self):
        """Even when the barrier times out, the inline fetch still runs."""
        denial = _fake_denial()
        fetch = AsyncMock(return_value="generated")
        with patch(
            "fighthealthinsurance.context_barrier._any_field_populated",
            AsyncMock(return_value=False),
        ):
            result = await warm_then_fetch(
                denial,
                readiness_fields=["ml_citation_context"],
                refresh_fields=["ml_citation_context"],
                barrier_timeout=0.05,
                fetch=lambda: fetch(),
                poll_interval=0.01,
            )
        self.assertEqual(result, "generated")
        fetch.assert_awaited_once()
        denial.arefresh_from_db.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

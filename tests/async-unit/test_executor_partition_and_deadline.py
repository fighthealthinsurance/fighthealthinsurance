"""Executor partitioning and the cooperative generation deadline.

- The interactive and background pools are separate (speculative precompute
  can never queue ahead of a waiting user) and env-sizable.
- parallel_infer submits to the pool it is handed.
- _checked_infer exits almost immediately once the requester's deadline has
  passed (threads can't be cancelled; this is what drains abandoned work).
- fire_and_forget_in_new_threadpool returns its thread's DB connections.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from fighthealthinsurance import exec as fhi_exec
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.utils import (
    fire_and_forget_in_new_threadpool,
    join_fire_and_forget_threads,
)


class TestPoolConfiguration:
    def test_pools_are_distinct(self):
        assert fhi_exec.executor is not fhi_exec.background_executor
        assert fhi_exec.executor is not fhi_exec.pubmed_executor

    def test_default_sizes(self):
        assert fhi_exec.executor._max_workers == 24
        assert fhi_exec.background_executor._max_workers == 8
        assert fhi_exec.pubmed_executor._max_workers == 4

    def test_pool_size_env_override(self, monkeypatch):
        monkeypatch.setenv("FHI_TEST_POOL_SIZE", "7")
        assert fhi_exec._pool_size("FHI_TEST_POOL_SIZE", 3) == 7

    def test_pool_size_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("FHI_TEST_POOL_SIZE", "banana")
        assert fhi_exec._pool_size("FHI_TEST_POOL_SIZE", 3) == 3

    def test_pool_size_floor_of_one(self, monkeypatch):
        monkeypatch.setenv("FHI_TEST_POOL_SIZE", "0")
        assert fhi_exec._pool_size("FHI_TEST_POOL_SIZE", 3) == 1


class _CountingExecutor(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=2)
        self.submissions = 0

    def submit(self, *args, **kwargs):
        self.submissions += 1
        return super().submit(*args, **kwargs)


class TestParallelInferPoolSelection:
    def _model(self):
        m = RemoteFullOpenLike("http://pool.test/v1", "tok", "pool-model")
        m.system_prompts_map = {"full": ["sys"], "full_not_patient": ["sys"]}
        return m

    def test_uses_the_supplied_executor(self):
        m = self._model()
        pool = _CountingExecutor()
        with patch.object(m, "_blocking_checked_infer", return_value=[]):
            futures = m.parallel_infer(
                prompt="p",
                infer_type="full",
                patient_context=None,
                plan_context=None,
                pubmed_context=None,
                submit_executor=pool,
            )
            for f in futures:
                f.result(timeout=10)
        assert pool.submissions == len(futures) > 0
        pool.shutdown(wait=True)

    def test_deadline_rides_along_to_the_blocking_call(self):
        m = self._model()
        pool = _CountingExecutor()
        seen = []

        def record(**kwargs):
            seen.append(kwargs.get("deadline"))
            return []

        with patch.object(m, "_blocking_checked_infer", side_effect=record):
            futures = m.parallel_infer(
                prompt="p",
                infer_type="full",
                patient_context=None,
                plan_context=None,
                pubmed_context=None,
                submit_executor=pool,
                deadline=12345.0,
            )
            for f in futures:
                f.result(timeout=10)
        assert seen and all(d == 12345.0 for d in seen)
        pool.shutdown(wait=True)


class TestCheckedInferDeadline:
    @pytest.mark.asyncio
    async def test_expired_deadline_short_circuits_with_no_model_calls(self):
        m = RemoteFullOpenLike("http://dl.test/v1", "tok", "dl-model")
        calls = []

        async def fake_infer_no_context(**kwargs):
            calls.append(kwargs)
            return "text"

        with patch.object(m, "_infer_no_context", side_effect=fake_infer_no_context):
            start = time.monotonic()
            result = await m._checked_infer(
                prompt="p",
                patient_context=None,
                plan_context=None,
                infer_type="full",
                pubmed_context=None,
                system_prompt="sys",
                temperature=0.7,
                deadline=time.monotonic() - 1.0,
            )
        assert result == []
        assert calls == []
        assert time.monotonic() - start < 1.0

    @pytest.mark.asyncio
    async def test_deadline_expiry_skips_the_bad_result_retry(self):
        m = RemoteFullOpenLike("http://dl.test/v1", "tok", "dl-model")
        calls = []
        deadline = time.monotonic() + 0.05

        async def slow_bad_result(**kwargs):
            calls.append(kwargs)
            await asyncio.sleep(0.1)  # pushes past the deadline
            return "x"  # bad (too short) for infer_type=full

        with patch.object(m, "_infer_no_context", side_effect=slow_bad_result):
            result = await m._checked_infer(
                prompt="p",
                patient_context=None,
                plan_context=None,
                infer_type="full",
                pubmed_context=None,
                system_prompt="sys",
                temperature=0.7,
                deadline=deadline,
            )
        assert result == []
        # Initial call happened; the one-retry was skipped by the deadline.
        assert len(calls) == 1


class TestFireAndForgetConnectionHygiene:
    @pytest.mark.asyncio
    async def test_db_connections_closed_after_task(self):
        async def tiny_task():
            await asyncio.sleep(0.01)

        with patch("django.db.close_old_connections") as close_mock:
            await fire_and_forget_in_new_threadpool(tiny_task())
            # Drain the background thread; run in a worker so the joins don't
            # block this test's event loop.
            await asyncio.to_thread(join_fire_and_forget_threads, 10.0)
        close_mock.assert_called()

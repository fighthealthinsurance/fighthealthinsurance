import pytest
import asyncio
import gc
import inspect
import threading
import time
import warnings
from typing import Awaitable, TypeVar, Any

from fighthealthinsurance.utils import (
    STAGE_AFTER_DELAY,
    STAGE_EARLY,
    STAGE_IMMEDIATE,
    STAGE_SKIPPED,
    StagedStart,
    fire_and_forget_in_new_threadpool,
    best_two_within_timelimit,
    best_within_timelimit,
    best_within_timelimit_static,
    execute_critical_optional_fireandforget,
)

T = TypeVar("T")


class TestAsyncTaskUtils:
    async def async_task_with_delay(self, result: T, delay: float) -> T:
        """Helper: Returns a result after specified delay"""
        await asyncio.sleep(delay)
        return result

    async def async_task_that_fails(self, delay: float = 0.1) -> None:
        """Helper: Task that raises an exception after delay"""
        await asyncio.sleep(delay)
        raise ValueError("Task failed deliberately")

    # Tests for fire_and_forget_in_new_threadpool
    @pytest.mark.asyncio
    async def test_fire_and_forget_in_new_threadpool(self):
        """Test that fire_and_forget_in_new_threadpool runs tasks without blocking"""
        # Create a shared variable and event to verify task execution
        # Use threading.Event since the background task runs in a separate thread
        shared_result = {"completed": False}
        done_event = threading.Event()

        async def background_task() -> None:
            await asyncio.sleep(0.5)
            shared_result["completed"] = True
            done_event.set()

        # Fire and forget
        await fire_and_forget_in_new_threadpool(background_task())

        # This should return immediately while task runs in background
        assert shared_result["completed"] is False

        # Wait for thread-safe event from background thread
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, done_event.wait, 5.0), timeout=10.0
        )
        assert shared_result["completed"] is True

    @pytest.mark.asyncio
    async def test_fire_and_forget_in_new_threadpool_exception_handling(self):
        """Test that exceptions in fire_and_forget tasks don't crash the program"""
        # Use threading.Event since background task runs in a separate thread
        completion_event = threading.Event()

        async def failing_task() -> None:
            try:
                await asyncio.sleep(0.1)
                raise ValueError("Task failed deliberately")
            finally:
                completion_event.set()

        await fire_and_forget_in_new_threadpool(failing_task())

        # Wait for the background task to finish (including exception handling)
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, completion_event.wait, 5.0), timeout=10.0
        )

        # Test passes if we reach here without crashing

    # Tests for best_within_timelimit
    @pytest.mark.asyncio
    async def test_best_within_timelimit_basic(self):
        """Test that best_within_timelimit returns the highest scored result"""
        tasks = [
            self.async_task_with_delay("fast_low_score", 0.1),
            self.async_task_with_delay("medium_best_score", 0.2),
            self.async_task_with_delay("slow_medium_score", 0.3),
        ]

        # Score function that considers both result and task
        def score_fn(result: str, _: Awaitable[str]) -> float:
            scores = {
                "fast_low_score": 1.0,
                "medium_best_score": 3.0,
                "slow_medium_score": 2.0,
            }
            return scores.get(result, 0.0)

        result = await best_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result == "medium_best_score"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_timeout(self):
        """Test that best_within_timelimit respects timeout and returns best available"""
        tasks = [
            self.async_task_with_delay("fast_low_score", 0.1),
            self.async_task_with_delay("medium_score", 0.3),
            self.async_task_with_delay("best_score_but_too_slow", 2.0),
        ]

        def score_fn(result: str, _: Awaitable[str]) -> float:
            scores = {
                "fast_low_score": 1.0,
                "medium_score": 2.0,
                "best_score_but_too_slow": 5.0,
            }
            return scores.get(result, 0.0)

        # With timeout of 1.0, the best score task (2.0s) won't complete in time
        result = await best_within_timelimit(tasks, score_fn, timeout=1.0)
        assert (
            result == "medium_score"
        )  # Medium score should be chosen as best available

    @pytest.mark.asyncio
    async def test_best_within_timelimit_uses_task_parameter(self):
        """Test that the score_fn can use the original task parameter"""
        # Create tasks with different contexts
        tasks = [
            self.async_task_with_delay("result1", 0.1),
            self.async_task_with_delay("result2", 0.1),
        ]

        # Store task references for lookup in score_fn
        task_scores = {tasks[0]: 1.0, tasks[1]: 2.0}

        # Score function that only considers the original task
        def score_fn(_: str, task: Awaitable[str]) -> float:
            return task_scores.get(task, 0.0)

        result = await best_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result == "result2"  # Task with higher score should be chosen

    @pytest.mark.asyncio
    async def test_best_within_timelimit_empty_list(self):
        """Test that best_within_timelimit handles empty task list properly"""
        result = await best_within_timelimit([], lambda r, _: 1.0, timeout=0.1)
        assert result is None

    @pytest.mark.asyncio
    async def test_neg_inf_scored_result_never_wins(self):
        """A truthy result scored -inf (hard-rejected) must not be returned
        when a valid result exists — even if the rejected one finishes first.

        Guards the chat loop fix: repeated replies are scored -inf, and
        before this semantic a truthy -inf result could still occupy the
        best slot via the "nothing better yet" fallback.
        """
        tasks = [
            self.async_task_with_delay("rejected_repeat", 0.05),
            self.async_task_with_delay("valid_answer", 0.2),
        ]

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return float("-inf") if result == "rejected_repeat" else 1.0

        result = await best_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result == "valid_answer"

    @pytest.mark.asyncio
    async def test_all_neg_inf_returns_none(self):
        """When every completed result is hard-rejected, the caller gets None
        (so its retry ladder runs) instead of a rejected result."""
        tasks = [
            self.async_task_with_delay("repeat_a", 0.05),
            self.async_task_with_delay("repeat_b", 0.1),
        ]

        result = await best_within_timelimit(
            tasks, lambda r, _: float("-inf"), timeout=1.0, extended_timeout=0.2
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_neg_inf_skipped_in_overtime_window(self):
        """In the overtime window the first USABLE result wins — a rejected
        (-inf) straggler must not end the wait."""
        tasks = [
            self.async_task_with_delay("rejected", 0.3),
            self.async_task_with_delay("valid_late", 0.6),
        ]

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return float("-inf") if result == "rejected" else 1.0

        # Main window (0.1s) sees nothing; overtime sees the rejected result
        # first and must keep waiting for the valid one.
        result = await best_within_timelimit(
            tasks, score_fn, timeout=0.1, extended_timeout=5.0
        )
        assert result == "valid_late"

    # Tests for best_two_within_timelimit
    @pytest.mark.asyncio
    async def test_best_two_returns_best_and_runner_up(self):
        tasks = [
            self.async_task_with_delay("low", 0.05),
            self.async_task_with_delay("best", 0.1),
            self.async_task_with_delay("middle", 0.15),
        ]
        scores = {"low": 1.0, "best": 3.0, "middle": 2.0}

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return scores.get(result, 0.0)

        result = await best_two_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result.best == "best"
        assert result.runner_up == "middle"

    @pytest.mark.asyncio
    async def test_best_two_reports_scores_and_tasks(self):
        """The result carries both scores and the ORIGINAL awaitables, so
        callers can judge how closely tied the race was and attribute each
        answer to the call that produced it."""
        tasks = [
            self.async_task_with_delay("best", 0.05),
            self.async_task_with_delay("middle", 0.1),
        ]
        scores = {"best": 3.0, "middle": 2.5}

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return scores.get(result, 0.0)

        result = await best_two_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result.best_score == 3.0
        assert result.runner_up_score == 2.5

    @pytest.mark.asyncio
    async def test_best_two_reports_originating_tasks(self):
        """The ORIGINAL awaitables come back, so a caller can map a winner to
        whatever it keyed its calls by (the chat layer maps them to models)."""
        tasks = [
            self.async_task_with_delay("best", 0.05),
            self.async_task_with_delay("middle", 0.1),
        ]
        scores = {"best": 3.0, "middle": 2.5}

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return scores.get(result, 0.0)

        result = await best_two_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result.best_task is tasks[0]
        assert result.runner_up_task is tasks[1]

    @pytest.mark.asyncio
    async def test_best_two_keeps_index_zero_as_best(self):
        """Field order is load-bearing: older callers index result[0]."""
        tasks = [self.async_task_with_delay("best", 0.05)]

        result = await best_two_within_timelimit(tasks, lambda r, _: 1.0, timeout=2.0)
        assert result[0] == "best"

    @pytest.mark.asyncio
    async def test_best_two_single_task_has_no_runner_up(self):
        tasks = [self.async_task_with_delay("only", 0.05)]
        result = await best_two_within_timelimit(tasks, lambda r, _: 1.0, timeout=1.0)
        assert result.best == "only"
        assert result.runner_up is None
        assert result.runner_up_task is None

    @pytest.mark.asyncio
    async def test_best_two_runner_up_never_neg_inf(self):
        """A hard-rejected result can't be the runner-up either."""
        tasks = [
            self.async_task_with_delay("best", 0.05),
            self.async_task_with_delay("rejected", 0.1),
        ]

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return float("-inf") if result == "rejected" else 1.0

        result = await best_two_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result.best == "best"
        assert result.runner_up is None

    @pytest.mark.asyncio
    async def test_best_two_runner_up_skips_duplicates_of_best(self):
        """Several backends serving the same model return identical answers;
        the runner-up must be the best DIFFERENT result, not a duplicate."""
        tasks = [
            self.async_task_with_delay("same_answer", 0.05),
            self.async_task_with_delay("same_answer", 0.1),
            self.async_task_with_delay("different_answer", 0.15),
        ]
        scores = {"same_answer": 3.0, "different_answer": 1.0}

        def score_fn(result: str, _: Awaitable[str]) -> float:
            return scores.get(result, 0.0)

        result = await best_two_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result.best == "same_answer"
        assert result.runner_up == "different_answer"

    @pytest.mark.asyncio
    async def test_best_two_all_duplicates_no_runner_up(self):
        tasks = [
            self.async_task_with_delay("same_answer", 0.05),
            self.async_task_with_delay("same_answer", 0.1),
        ]
        result = await best_two_within_timelimit(tasks, lambda r, _: 1.0, timeout=2.0)
        assert result.best == "same_answer"
        assert result.runner_up is None

    @pytest.mark.asyncio
    async def test_best_within_timelimit_with_exceptions(self):
        """Test that best_within_timelimit handles task exceptions properly"""
        tasks = [
            self.async_task_that_fails(0.1),
            self.async_task_with_delay("valid_result", 0.2),
        ]

        def score_fn(result: str, _: Awaitable[Any]) -> float:
            return 1.0  # Simple scoring

        # Should skip the failing task and return the valid one
        result = await best_within_timelimit(tasks, score_fn, timeout=2.0)
        assert result == "valid_result"

    # Tests for best_within_timelimit_static
    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_basic(self):
        """Test that best_within_timelimit_static works with static scores"""
        task1 = self.async_task_with_delay("result1", 0.1)
        task2 = self.async_task_with_delay("result2", 0.2)
        task3 = self.async_task_with_delay("result3", 0.3)

        # Define static scores for each task
        task_scores = {
            task1: 1.0,
            task2: 3.0,  # Highest score
            task3: 2.0,
        }

        result = await best_within_timelimit_static(task_scores, timeout=2.0)
        assert result == "result2"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_timeout(self):
        """Test that best_within_timelimit_static respects timeout"""
        task1 = self.async_task_with_delay("fast", 0.1)
        task2 = self.async_task_with_delay("slow_but_best", 2.0)

        task_scores = {
            task1: 1.0,
            task2: 2.0,  # Higher score but too slow
        }

        # With timeout of 0.5, only task1 should complete
        result = await best_within_timelimit_static(task_scores, timeout=0.5)
        assert result == "fast"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_empty_dict(self):
        """Test best_within_timelimit_static with empty dictionary"""
        with pytest.raises(ValueError, match="No tasks provided"):
            await best_within_timelimit_static({}, timeout=0.1)

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_early_return_best_task(self):
        """Test that best_within_timelimit_static returns early when a best task finishes"""
        # Create three tasks with different scores and completion times
        task_fast_low = self.async_task_with_delay("fast_low", 0.1)
        task_medium_best = self.async_task_with_delay("medium_best", 0.3)
        task_slow_medium = self.async_task_with_delay("slow_medium", 2.0)

        task_scores = {
            task_fast_low: 1.0,
            task_medium_best: 3.0,  # Best score
            task_slow_medium: 2.0,
        }

        start_time = time.time()
        result = await best_within_timelimit_static(task_scores, timeout=5.0)
        elapsed_time = time.time() - start_time

        # Should return medium_best as soon as it's ready (around 0.3s)
        # Without waiting for slow_medium
        assert result == "medium_best"
        assert elapsed_time < 2.0  # Allow generous headroom for slow CI

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_equal_max_scores(self):
        """Test handling multiple tasks with the same max score (return first to finish)"""
        # Two tasks with equal best score but different completion times
        task_fast_best = self.async_task_with_delay("fast_best", 0.1)
        task_slow_best = self.async_task_with_delay("slow_best", 1.0)
        task_medium_low = self.async_task_with_delay("medium_low", 0.3)

        task_scores = {
            task_fast_best: 3.0,  # Tied for best
            task_slow_best: 3.0,  # Tied for best
            task_medium_low: 1.0,
        }

        result = await best_within_timelimit_static(task_scores, timeout=2.0)

        # Should return the first best task to finish (fast_best)
        assert result == "fast_best"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_best_task_timeout(self):
        """Test that function returns best completed task when best task times out"""
        # Best task is too slow, medium task should be returned
        task_fast_low = self.async_task_with_delay("fast_low", 0.1)
        task_medium = self.async_task_with_delay("medium", 0.3)
        task_slow_best = self.async_task_with_delay("slow_best", 2.0)

        task_scores = {
            task_fast_low: 1.0,
            task_medium: 2.0,
            task_slow_best: 3.0,  # Best score but too slow
        }

        result = await best_within_timelimit_static(task_scores, timeout=1.0)

        # Should return the best task that completed within timeout
        assert result == "medium"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_all_tasks_fail(self):
        """Test when all tasks fail with exceptions"""
        # Create tasks that all fail
        task1 = self.async_task_that_fails(0.1)
        task2 = self.async_task_that_fails(0.2)

        task_scores = {
            task1: 1.0,
            task2: 2.0,
        }

        # Should raise ValueError when all tasks fail
        with pytest.raises(ValueError, match="No tasks completed successfully"):
            await best_within_timelimit_static(task_scores, timeout=2.0)

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_best_task_fails(self):
        """Test when the highest-scored task fails"""
        # Best task fails, should return next best
        task_ok = self.async_task_with_delay("ok_result", 0.1)
        task_fail = self.async_task_that_fails(0.3)

        task_scores = {
            task_ok: 1.0,
            task_fail: 2.0,  # Higher score but fails
        }

        result = await best_within_timelimit_static(task_scores, timeout=2.0)
        assert result == "ok_result"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_all_timeout_with_next_completion(self):
        """Test when all tasks time out initially but we wait for next completion"""
        # All tasks exceed initial timeout but one completes soon after
        task_slow = self.async_task_with_delay("slow", 0.5)
        task_very_slow = self.async_task_with_delay("very_slow", 2.0)

        task_scores = {
            task_slow: 1.0,
            task_very_slow: 2.0,
        }

        # With timeout of 0.3, both exceed initial timeout but we should get slow task
        # with extended timeout of 1.0
        result = await best_within_timelimit_static(
            task_scores, timeout=0.3, extended_timeout=1.0
        )
        assert result == "slow"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_extended_timeout_no_completion(self):
        """Test that extended_timeout too short causes failure.

        When no tasks complete within initial timeout and extended timeout is
        also too short, ValueError should be raised.
        """
        task_medium = self.async_task_with_delay("medium", 1.0)
        task_slow = self.async_task_with_delay("slow", 2.0)

        task_scores = {
            task_medium: 1.0,
            task_slow: 2.0,
        }

        # With initial timeout of 0.1 and extended timeout of 0.2,
        # neither task completes (both need at least 1.0s total)
        with pytest.raises(ValueError, match="No tasks completed successfully"):
            await best_within_timelimit_static(
                task_scores, timeout=0.1, extended_timeout=0.2
            )

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_extended_timeout_first_completed(self):
        """Test that extended_timeout long enough allows first task to complete.

        When no tasks complete within initial timeout, the function uses
        FIRST_COMPLETED in extended timeout - returning the first task to complete,
        regardless of score.
        """
        task_medium = self.async_task_with_delay("medium", 0.5)
        task_slow = self.async_task_with_delay("slow", 2.0)

        task_scores = {
            task_medium: 1.0,
            task_slow: 2.0,
        }

        # With initial timeout of 0.1 but extended timeout of 1.5,
        # medium finishes first (at ~0.4s into extended) and is returned
        # because FIRST_COMPLETED is used in the extended timeout period
        result = await best_within_timelimit_static(
            task_scores, timeout=0.1, extended_timeout=1.5
        )
        assert result == "medium"

    # Tests for execute_critical_optional_fireandforget
    @pytest.mark.asyncio
    async def test_execute_critical_optional_fireandforget_basic(self):
        """Test basic functionality of execute_critical_optional_fireandforget"""
        # Setup test state
        shared_state = {
            "critical1": False,
            "critical2": False,
            "optional1": False,
            "optional2": False,
            "fireforget1": False,
        }

        async def critical_task1():
            await asyncio.sleep(0.1)
            shared_state["critical1"] = True
            return "critical1_result"

        async def critical_task2():
            await asyncio.sleep(0.2)
            shared_state["critical2"] = True
            return "critical2_result"

        async def optional_task1():
            await asyncio.sleep(0.3)
            shared_state["optional1"] = True
            return "optional1_result"

        async def optional_task2():
            await asyncio.sleep(0.4)
            shared_state["optional2"] = True
            return "optional2_result"

        async def fire_forget_task():
            await asyncio.sleep(0.1)
            shared_state["fireforget1"] = True

        # Execute tasks - function returns an async iterator
        critical = [critical_task1(), critical_task2()]
        optional = [optional_task1(), optional_task2()]
        fire_forget = [fire_forget_task()]

        results = []
        async for result in execute_critical_optional_fireandforget(
            critical, optional, fire_forget
        ):
            results.append(result)

        # Critical tasks should be complete
        assert len(results) >= 2
        assert "critical1_result" in results
        assert "critical2_result" in results
        assert shared_state["critical1"] is True
        assert shared_state["critical2"] is True

        # Wait long enough to let fire_forget task finish
        await asyncio.sleep(1.0)
        assert shared_state["fireforget1"] is True

    @pytest.mark.asyncio
    async def test_execute_critical_optional_fireandforget_with_exceptions(self):
        """Test that execute_critical_optional_fireandforget handles exceptions in critical tasks"""

        async def critical_success():
            return "success"

        async def critical_failure():
            raise ValueError("Critical task failed")

        critical = [critical_success(), critical_failure()]
        optional = []
        fire_forget = []

        # Iterate through results - exceptions from required tasks are
        # caught and logged internally by the generator, not propagated
        results = []
        async for result in execute_critical_optional_fireandforget(
            critical, optional, fire_forget
        ):
            results.append(result)

        # At least the successful task should have completed
        assert "success" in results


class TestBestWithinTimelimitOvertime:
    """The bounded overtime window that replaced the old ((timeout+1)*20)
    unbounded tail: slow-but-successful stragglers still land, exhaustion
    returns None instead of raising, and no exit path leaks running tasks."""

    @pytest.mark.asyncio
    async def test_slow_success_in_overtime_window_is_returned(self):
        async def slow_success() -> str:
            await asyncio.sleep(0.5)
            return "late but good"

        result = await best_within_timelimit(
            [slow_success()],
            lambda r, _t: 1.0,
            timeout=0.1,
            extended_timeout=5.0,
        )
        assert result == "late but good"

    @pytest.mark.asyncio
    async def test_exhaustion_returns_none_instead_of_raising(self):
        async def fails() -> str:
            await asyncio.sleep(0.05)
            raise ValueError("nope")

        result = await best_within_timelimit(
            [fails(), fails()],
            lambda r, _t: 1.0,
            timeout=0.5,
            extended_timeout=0.5,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_nothing_completes_returns_none_at_deadline(self):
        async def never_finishes() -> str:
            await asyncio.sleep(30)
            return "way too late"

        start = time.monotonic()
        result = await best_within_timelimit(
            [never_finishes()],
            lambda r, _t: 1.0,
            timeout=0.1,
            extended_timeout=0.3,
        )
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 5.0  # nowhere near the old 20x tail

    @pytest.mark.asyncio
    async def test_extended_timeout_zero_is_strict(self):
        async def slowish() -> str:
            await asyncio.sleep(0.5)
            return "late"

        start = time.monotonic()
        result = await best_within_timelimit(
            [slowish()],
            lambda r, _t: 1.0,
            timeout=0.1,
            extended_timeout=0.0,
        )
        assert result is None
        assert time.monotonic() - start < 0.4

    @pytest.mark.asyncio
    async def test_pending_tasks_cancelled_after_result(self):
        cancelled = asyncio.Event()

        async def fast() -> str:
            await asyncio.sleep(0.05)
            return "winner"

        async def hangs() -> str:
            try:
                await asyncio.sleep(30)
                return "never"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        result = await best_within_timelimit(
            [fast(), hangs()],
            lambda r, _t: 1.0,
            timeout=0.5,
        )
        assert result == "winner"
        # Cancellation is fire-and-forget; give the loop a beat to run it.
        await asyncio.wait_for(cancelled.wait(), timeout=5.0)

    @pytest.mark.asyncio
    async def test_falsy_first_overtime_completion_keeps_waiting(self):
        """The old implementation raised as soon as the first overtime
        completion was falsy, abandoning still-running tasks that would have
        succeeded. The rewrite keeps waiting for the next completion."""

        async def early_but_empty() -> str:
            await asyncio.sleep(0.15)
            return ""

        async def later_and_good() -> str:
            await asyncio.sleep(0.3)
            return "good"

        result = await best_within_timelimit(
            [early_but_empty(), later_and_good()],
            lambda r, _t: 1.0 if r else 0.0,
            timeout=0.05,
            extended_timeout=5.0,
        )
        assert result == "good"


class TestBestWithinTimelimitCancellation:
    """Cancelling the CALLER (e.g. the chat turn budget expiring) must cancel
    the fan-out tasks too: asyncio.wait/as_completed never cancel their
    children on their own, so without explicit propagation the model calls
    kept running to their individual timeouts."""

    @pytest.mark.asyncio
    async def test_outer_cancellation_cancels_fanout_tasks(self):
        child_cancelled = asyncio.Event()

        async def hangs() -> str:
            try:
                await asyncio.sleep(30)
                return "never"
            except asyncio.CancelledError:
                child_cancelled.set()
                raise

        outer = asyncio.create_task(
            best_within_timelimit([hangs()], lambda r, _t: 1.0, timeout=10)
        )
        await asyncio.sleep(0.1)  # let the fan-out start
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.wait_for(child_cancelled.wait(), timeout=5.0)

    @pytest.mark.asyncio
    async def test_static_outer_cancellation_cancels_fanout_tasks(self):
        child_cancelled = asyncio.Event()

        async def hangs() -> str:
            try:
                await asyncio.sleep(30)
                return "never"
            except asyncio.CancelledError:
                child_cancelled.set()
                raise

        outer = asyncio.create_task(
            best_within_timelimit_static({hangs(): 1.0}, timeout=10)
        )
        await asyncio.sleep(0.1)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.wait_for(child_cancelled.wait(), timeout=5.0)


# --- Staged start: held-back tasks (the chat fan-out's "ours first") ----------


class _Probe:
    """Coroutines that note when (and whether) they started running."""

    def __init__(self):
        self.started = {}

    async def call(self, name, result, delay, fail=False):
        self.started[name] = asyncio.get_running_loop().time()
        await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("backend down")
        return result


def _scores(table):
    def score_fn(result, _task):
        return table.get(result, 1.0)

    return score_fn


def _never_awaited_warnings(caught):
    return [w for w in caught if "never awaited" in str(w.message)]


class TestStagedStart:
    @pytest.mark.asyncio
    async def test_a_usable_answer_of_ours_means_the_held_back_tasks_never_start(
        self,
    ):
        probe = _Probe()
        ours = probe.call("ours", "ours-answer", 0.05)
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        loop = asyncio.get_running_loop()
        started = loop.time()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = await best_two_within_timelimit(
                [ours, theirs],
                _scores({}),
                timeout=2.0,
                extended_timeout=0.0,
                deferred=[theirs],
                defer_seconds=1.0,
                stage=stage,
            )
            assert inspect.getcoroutinestate(theirs) == inspect.CORO_CLOSED
            del theirs
            stage.skipped.clear()
            gc.collect()

        assert result.best == "ours-answer"
        assert "theirs" not in probe.started
        assert stage.outcome == STAGE_SKIPPED
        assert stage.started_after is None
        # It did not wait out the delay.
        assert loop.time() - started < 0.5
        assert _never_awaited_warnings(caught) == []

    @pytest.mark.asyncio
    async def test_the_skipped_tasks_are_reported(self):
        probe = _Probe()
        ours = probe.call("ours", "ours-answer", 0.01)
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        await best_two_within_timelimit(
            [ours, theirs],
            _scores({}),
            timeout=2.0,
            deferred=[theirs],
            defer_seconds=1.0,
            stage=stage,
        )
        assert stage.skipped == [theirs]

    @pytest.mark.asyncio
    async def test_the_held_back_tasks_start_when_the_delay_passes(self):
        probe = _Probe()
        loop = asyncio.get_running_loop()
        race_start = loop.time()
        ours = probe.call("ours", "ours-late", 0.8)
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        result = await best_two_within_timelimit(
            [ours, theirs],
            _scores({"ours-late": 5.0, "their-answer": 1.0}),
            timeout=2.0,
            extended_timeout=0.0,
            deferred=[theirs],
            defer_seconds=0.2,
            stage=stage,
        )
        assert stage.outcome == STAGE_AFTER_DELAY
        assert 0.15 <= probe.started["theirs"] - race_start < 0.5
        assert 0.15 <= stage.started_after < 0.5
        # Ours still finished inside the main window and outscored theirs.
        assert result.best == "ours-late"
        assert result.runner_up == "their-answer"

    @pytest.mark.asyncio
    async def test_the_held_back_tasks_start_early_once_ours_have_all_failed(self):
        probe = _Probe()
        loop = asyncio.get_running_loop()
        race_start = loop.time()
        failing = probe.call("failing", None, 0.05, fail=True)
        empty = probe.call("empty", "", 0.02)
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        result = await best_two_within_timelimit(
            [failing, empty, theirs],
            _scores({}),
            timeout=10.0,
            deferred=[theirs],
            defer_seconds=5.0,
            stage=stage,
        )
        assert stage.outcome == STAGE_EARLY
        assert result.best == "their-answer"
        assert probe.started["theirs"] - race_start < 0.5

    @pytest.mark.asyncio
    async def test_a_rejected_answer_of_ours_counts_as_failed(self):
        probe = _Probe()
        looped = probe.call("looped", "looped-reply", 0.02)
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        result = await best_two_within_timelimit(
            [looped, theirs],
            _scores({"looped-reply": float("-inf")}),
            timeout=10.0,
            deferred=[theirs],
            defer_seconds=5.0,
            stage=stage,
        )
        assert stage.outcome == STAGE_EARLY
        assert result.best == "their-answer"

    @pytest.mark.asyncio
    async def test_a_delay_of_zero_is_the_unstaged_race(self):
        async def run(**staging):
            probe = _Probe()
            loop = asyncio.get_running_loop()
            race_start = loop.time()
            calls = [
                probe.call("a", "answer-a", 0.03),
                probe.call("theirs", "answer-b", 0.01),
                probe.call("c", "answer-c", 0.02),
            ]
            if staging:
                staging["deferred"] = [calls[1]]
            result = await best_two_within_timelimit(
                calls,
                _scores({"answer-a": 2.0, "answer-b": 3.0, "answer-c": 2.0}),
                timeout=2.0,
                **staging,
            )
            return result, probe, race_start, calls

        plain, _probe, _start, plain_calls = await run()
        stage = StagedStart()
        zero, probe, race_start, zero_calls = await run(defer_seconds=0.0, stage=stage)
        assert (zero.best, zero.runner_up, zero.best_score, zero.runner_up_score) == (
            plain.best,
            plain.runner_up,
            plain.best_score,
            plain.runner_up_score,
        )
        assert zero.best_task is zero_calls[1]
        assert plain.best_task is plain_calls[1]
        assert stage.outcome == STAGE_IMMEDIATE
        # Every task started at once.
        assert max(probe.started.values()) - race_start < 0.05

    @pytest.mark.asyncio
    async def test_nothing_is_held_back_when_nothing_else_would_start_first(self):
        probe = _Probe()
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        result = await best_two_within_timelimit(
            [theirs],
            _scores({}),
            timeout=2.0,
            deferred=[theirs],
            defer_seconds=5.0,
            stage=stage,
        )
        assert result.best == "their-answer"
        assert stage.outcome == STAGE_IMMEDIATE

    @pytest.mark.asyncio
    async def test_a_staged_race_fits_inside_the_same_windows(self):
        """The delay comes out of the main window rather than adding to it,
        and is capped at it, so the race's longest run is unchanged."""
        loop = asyncio.get_running_loop()

        async def hangs():
            await asyncio.sleep(10)
            return "never"

        async def run(**staging):
            calls = [hangs(), hangs()]
            if staging:
                staging["deferred"] = [calls[1]]
            started = loop.time()
            result = await best_two_within_timelimit(
                calls, _scores({}), timeout=0.3, extended_timeout=0.2, **staging
            )
            return result, loop.time() - started

        plain, plain_elapsed = await run()
        stage = StagedStart()
        staged, staged_elapsed = await run(defer_seconds=5.0, stage=stage)
        assert plain.best is None and staged.best is None
        assert stage.outcome == STAGE_AFTER_DELAY
        # Started when the (capped) delay ran out: at the main window's end.
        assert 0.25 <= stage.started_after < 0.4
        assert staged_elapsed < 0.3 + 0.2 + 0.15
        assert staged_elapsed <= plain_elapsed + 0.1

    @pytest.mark.asyncio
    async def test_exact_ties_still_go_to_the_earlier_listed_task(self):
        """Results scored in different batches are still ranked in fan-out
        order: the doubled lead backend's two answers tie exactly."""
        probe = _Probe()
        first = probe.call("first", "answer-a", 0.1)
        second = probe.call("second", "answer-b", 0.02)
        theirs = probe.call("theirs", "their-answer", 0.01)
        result = await best_two_within_timelimit(
            [first, second, theirs],
            _scores({}),
            timeout=2.0,
            deferred=[theirs],
            defer_seconds=1.0,
            stage=StagedStart(),
        )
        assert result.best == "answer-a"
        assert result.best_task is first
        assert result.runner_up == "answer-b"

    @pytest.mark.asyncio
    async def test_cancelling_a_staged_race_closes_the_held_back_tasks(self):
        probe = _Probe()
        ours = probe.call("ours", "never", 30)
        theirs = probe.call("theirs", "their-answer", 0.01)
        stage = StagedStart()
        outer = asyncio.create_task(
            best_two_within_timelimit(
                [ours, theirs],
                _scores({}),
                timeout=10.0,
                deferred=[theirs],
                defer_seconds=5.0,
                stage=stage,
            )
        )
        await asyncio.sleep(0.05)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert inspect.getcoroutinestate(theirs) == inspect.CORO_CLOSED
        assert "theirs" not in probe.started
        assert stage.outcome == STAGE_SKIPPED
        assert stage.skipped == [theirs]

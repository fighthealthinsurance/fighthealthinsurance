import pytest
import asyncio
import time
from typing import Awaitable, TypeVar, Any

from fighthealthinsurance.utils import (
    fire_and_forget_in_new_threadpool,
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
        # Create a shared variable to verify task execution
        shared_result = {"completed": False}

        async def background_task() -> None:
            await asyncio.sleep(0.2)
            shared_result["completed"] = True

        # Fire and forget
        await fire_and_forget_in_new_threadpool(background_task())

        # This should return immediately while task runs in background
        assert shared_result["completed"] is False

        # Wait a bit longer to confirm task completes
        await asyncio.sleep(0.5)
        assert shared_result["completed"] is True

    @pytest.mark.asyncio
    async def test_fire_and_forget_in_new_threadpool_exception_handling(self):
        """Test that exceptions in fire_and_forget tasks don't crash the program"""
        # This would raise an exception, but shouldn't crash our test
        await fire_and_forget_in_new_threadpool(self.async_task_that_fails(0.1))

        # Wait to ensure the exception had time to be raised
        await asyncio.sleep(0.3)

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

        result = await best_within_timelimit(tasks, score_fn, timeout=0.5)
        assert result == "medium_best_score"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_timeout(self):
        """Test that best_within_timelimit respects timeout and returns best available"""
        tasks = [
            self.async_task_with_delay("fast_low_score", 0.1),
            self.async_task_with_delay("medium_score", 0.2),
            self.async_task_with_delay("best_score_but_too_slow", 0.5),
        ]

        def score_fn(result: str, _: Awaitable[str]) -> float:
            scores = {
                "fast_low_score": 1.0,
                "medium_score": 2.0,
                "best_score_but_too_slow": 5.0,
            }
            return scores.get(result, 0.0)

        # With timeout of 0.3, the best score task won't complete in time
        result = await best_within_timelimit(tasks, score_fn, timeout=0.3)
        assert result == "medium_score"  # Medium score should be chosen as best available

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

        result = await best_within_timelimit(tasks, score_fn, timeout=0.3)
        assert result == "result2"  # Task with higher score should be chosen

    @pytest.mark.asyncio
    async def test_best_within_timelimit_empty_list(self):
        """Test that best_within_timelimit handles empty task list properly"""
        result = await best_within_timelimit([], lambda r, _: 1.0, timeout=0.1)
        assert result is None

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
        result = await best_within_timelimit(tasks, score_fn, timeout=0.3)
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

        result = await best_within_timelimit_static(task_scores, timeout=0.5)
        assert result == "result2"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_timeout(self):
        """Test that best_within_timelimit_static respects timeout"""
        task1 = self.async_task_with_delay("fast", 0.1)
        task2 = self.async_task_with_delay("slow_but_best", 0.4)

        task_scores = {
            task1: 1.0,
            task2: 2.0,  # Higher score but too slow
        }

        # With timeout of 0.2, only task1 should complete
        result = await best_within_timelimit_static(task_scores, timeout=0.2)
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
        task_medium_best = self.async_task_with_delay("medium_best", 0.2)
        task_slow_medium = self.async_task_with_delay("slow_medium", 0.4)

        task_scores = {
            task_fast_low: 1.0,
            task_medium_best: 3.0,  # Best score
            task_slow_medium: 2.0,
        }

        start_time = time.time()
        result = await best_within_timelimit_static(task_scores, timeout=1.0)
        elapsed_time = time.time() - start_time

        # Should return medium_best as soon as it's ready (around 0.2s)
        # Without waiting for slow_medium
        assert result == "medium_best"
        assert elapsed_time < 0.3  # Allow small overhead

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_equal_max_scores(self):
        """Test handling multiple tasks with the same max score (return first to finish)"""
        # Two tasks with equal best score but different completion times
        task_fast_best = self.async_task_with_delay("fast_best", 0.1)
        task_slow_best = self.async_task_with_delay("slow_best", 0.3)
        task_medium_low = self.async_task_with_delay("medium_low", 0.2)

        task_scores = {
            task_fast_best: 3.0,  # Tied for best
            task_slow_best: 3.0,  # Tied for best
            task_medium_low: 1.0,
        }

        result = await best_within_timelimit_static(task_scores, timeout=0.5)

        # Should return the first best task to finish (fast_best)
        assert result == "fast_best"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_best_task_timeout(self):
        """Test that function returns best completed task when best task times out"""
        # Best task is too slow, medium task should be returned
        task_fast_low = self.async_task_with_delay("fast_low", 0.1)
        task_medium = self.async_task_with_delay("medium", 0.2)
        task_slow_best = self.async_task_with_delay("slow_best", 0.5)

        task_scores = {
            task_fast_low: 1.0,
            task_medium: 2.0,
            task_slow_best: 3.0,  # Best score but too slow
        }

        result = await best_within_timelimit_static(task_scores, timeout=0.3)

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
            await best_within_timelimit_static(task_scores, timeout=0.3)

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_best_task_fails(self):
        """Test when the highest-scored task fails"""
        # Best task fails, should return next best
        task_ok = self.async_task_with_delay("ok_result", 0.1)
        task_fail = self.async_task_that_fails(0.2)

        task_scores = {
            task_ok: 1.0,
            task_fail: 2.0,  # Higher score but fails
        }

        result = await best_within_timelimit_static(task_scores, timeout=0.3)
        assert result == "ok_result"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_all_timeout_with_next_completion(self):
        """Test when all tasks time out initially but we wait for next completion"""
        # All tasks exceed initial timeout but one completes soon after
        task_slow = self.async_task_with_delay("slow", 0.4)
        task_very_slow = self.async_task_with_delay("very_slow", 0.6)

        task_scores = {
            task_slow: 1.0,
            task_very_slow: 2.0,
        }

        # With timeout of 0.3, both exceed initial timeout but we should get slow task
        # with extended timeout of 0.5
        result = await best_within_timelimit_static(
            task_scores, timeout=0.3, extended_timeout=0.5
        )
        assert result == "slow"

    @pytest.mark.asyncio
    async def test_best_within_timelimit_static_extended_timeout_parameter(self):
        """Test the configurable extended_timeout parameter.

        Note: When no tasks complete within initial timeout, the function uses
        FIRST_COMPLETED in extended timeout - returning the first task to complete,
        regardless of score. This tests that behavior.
        """
        # Test 1: Extended timeout too short - neither task completes
        task_medium_1 = self.async_task_with_delay("medium", 0.4)
        task_slow_1 = self.async_task_with_delay("slow", 0.8)

        task_scores_1 = {
            task_medium_1: 1.0,
            task_slow_1: 2.0,
        }

        # With initial timeout of 0.1 and extended timeout of 0.1,
        # neither task completes (both need at least 0.4s total)
        with pytest.raises(ValueError, match="No tasks completed successfully"):
            await best_within_timelimit_static(
                task_scores_1, timeout=0.1, extended_timeout=0.1
            )

        # Test 2: Extended timeout long enough for faster task (fresh coroutines)
        task_medium_2 = self.async_task_with_delay("medium", 0.4)
        task_slow_2 = self.async_task_with_delay("slow", 0.8)

        task_scores_2 = {
            task_medium_2: 1.0,
            task_slow_2: 2.0,
        }

        # With initial timeout of 0.1 but extended timeout of 0.5,
        # medium finishes first (at ~0.3s into extended) and is returned
        # because FIRST_COMPLETED is used in the extended timeout period
        result = await best_within_timelimit_static(
            task_scores_2, timeout=0.1, extended_timeout=0.5
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

        # Wait a bit to let fire_forget task finish
        await asyncio.sleep(0.2)
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

        # Iterate through results - function returns an async iterator
        results = []
        try:
            async for result in execute_critical_optional_fireandforget(
                critical, optional, fire_forget
            ):
                results.append(result)
        except ValueError:
            # The exception propagates when iterating
            pass

        # At least the successful task should have completed
        assert "success" in results

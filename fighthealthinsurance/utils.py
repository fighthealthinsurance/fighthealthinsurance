from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings

import time
import random
import string
import asyncstdlib
import asyncio
from inspect import isabstract
import concurrent
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import reduce
from typing import (
    AsyncIterator,
    Iterator,
    List,
    Optional,
    TypeVar,
    Awaitable,
    Callable,
    Any,
    Generic,
    Dict,
    Tuple,
    Coroutine,
    Sequence,
    Union,
    cast,
    AsyncGenerator,
)
from uuid import UUID
from subprocess import CalledProcessError
from loguru import logger

import requests
from metapub import PubMedFetcher
from requests.exceptions import RequestException
from markdown_strings import esc_format

from fighthealthinsurance.env_utils import *

pubmed_fetcher = PubMedFetcher()

U = TypeVar("U")
T = TypeVar("T")

background_tasks: set[asyncio.Task[Any]] = set()

flat_map = lambda f, xs: reduce(lambda a, b: a + b, map(f, xs))

# Some pages return 200 where it should be 404 :(
common_bad_result = [
    "The page you are trying to reach is not available. Please check the URL and try again.",
    "The requested article is not currently available on this site.",
]

maybe_bad_url_endings = re.compile("^(.*)[\\.\\:\\;\\,\\?\\>]+$")


def is_convertible_to_int(s):
    try:
        int(s)
        return True
    except ValueError:
        return False


def send_fallback_email(subject: str, template_name: str, context, to_email: str):
    if to_email.endswith("-fake@fighthealthinsurance.com"):
        return
    # First, render the plain text content if present
    text_content = render_to_string(
        f"emails/{template_name}.txt",
        context=context,
    )

    # Secondly, render the HTML content if present
    html_content = render_to_string(
        f"emails/{template_name}.html",
        context=context,
    )
    # Then, create a multipart email instance.
    msg = EmailMultiAlternatives(
        subject,
        text_content,
        settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    logger.debug(f"Sending email to {to_email} with subject {subject}")

    # Lastly, attach the HTML content to the email instance and send.
    msg.attach_alternative(html_content, "text/html")
    msg.send()
    try:
        second_msg = EmailMultiAlternatives(
            subject + " -- " + to_email,
            text_content,
            settings.DEFAULT_FROM_EMAIL,
            to=settings.BCC_EMAILS,
        )
        second_msg.attach_alternative(html_content, "text/html")
        second_msg.send()
    except Exception as e:
        logger.error(f"Error sending email to BCC: {e}")
        pass


async def cancel_tasks(tasks: List[asyncio.Task]) -> None:
    """
    Cancel a list of asyncio tasks.
    """
    logger.debug(f"Cancelling {len(tasks)} tasks")
    for task in tasks:
        if not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)
    logger.debug("All tasks cancelled")


async def check_call(cmd, max_retries=0, **kwargs):
    logger.debug(f"Running: {cmd}")
    process = await asyncio.create_subprocess_exec(
        *cmd, **kwargs, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    return_code = await process.wait()
    if return_code != 0:
        if max_retries < 1:
            raise CalledProcessError(return_code, cmd)
        else:
            logger.debug(f"Retrying {cmd}")
            return await check_call(cmd, max_retries=max_retries - 1, **kwargs)
    else:
        logger.debug(f"Success {cmd}")


def markdown_escape(string: Optional[str]) -> str:
    if string is None:
        return ""
    result: str = esc_format(string, esc=True)
    return result


def sekret_gen():
    return str(UUID(bytes=os.urandom(16), version=4))


class UnwrapIterator(Iterator[T]):
    def __init__(self, iterators: Iterator[Iterator[T]]):
        self.iterators = iterators
        self.head: Optional[Iterator[T]] = None

    def __next__(self) -> T:
        if self.head is None:
            self.head = self.iterators.__next__()
        try:
            return self.head.__next__()
        except StopIteration:
            self.head = None
            return self.__next__()


def as_available_nested(futures: List[Future[Iterator[U]]]) -> Iterator[U]:
    iterators = as_available(futures)
    return UnwrapIterator(iterators)


def as_available(futures: List[Future[U]]) -> Iterator[U]:
    def complete(f: Future[U]) -> U:
        r = f.result()
        return r

    return map(complete, concurrent.futures.as_completed(futures))


def all_subclasses(cls: type[U]) -> set[type[U]]:
    return set(cls.__subclasses__()).union(
        [s for c in cls.__subclasses__() for s in all_subclasses(c)]
    )


def all_concrete_subclasses(cls: type[U]):
    return [c for c in all_subclasses(cls) if not isabstract(c)]


# I'm lazy and we only work with strings right now.


def interleave_iterator_for_keep_alive(
    iterator: AsyncIterator[str], timeout: int = 45
) -> AsyncIterator[str]:
    return asyncstdlib.iter(
        _interleave_iterator_for_keep_alive(iterator, timeout=timeout)
    )


async def _interleave_iterator_for_keep_alive(
    iterator: AsyncIterator[str], timeout: int = 20
) -> AsyncIterator[str]:
    """
    Yields strings from an async iterator, interleaving empty strings as keep-alive signals.

    This generator yields an empty string before and after each item from the input iterator,
    and also yields an empty string every `timeout` seconds if no new item is available.
    Handles timeouts, cancellations, and exceptions by yielding empty strings to maintain
    connection liveness.
    """
    yield ""
    await asyncio.sleep(0)
    # Keep track of the next elem pointer
    c = None
    while True:
        try:
            if c is None:
                # Keep wait_for from cancelling it
                c = asyncio.shield(iterator.__anext__())
            await asyncio.sleep(0)
            yield ""
            # Use asyncio.wait_for to handle timeout for fetching record
            record = await asyncio.wait_for(c, timeout)
            # Success, we can clear the next elem pointer
            c = None
            yield record
            await asyncio.sleep(0)
            yield ""
        except asyncio.TimeoutError:
            yield ""
            continue
        except asyncio.exceptions.CancelledError:
            # If the iterator is cancelled, we should stop
            logger.debug("Cancellation of task in interleaved generator")
            yield ""
            c = None
        except StopAsyncIteration:
            # Break the loop if iteration is complete
            break
        except Exception as e:
            logger.opt(exception=True).error(f"Error in generator: {e}")
            yield ""
            c = None


async def fire_and_forget_in_new_threadpool(task: Coroutine) -> None:
    """
    Runs an asynchronous coroutine in a separate thread with its own event loop.

    The coroutine is executed in a fire-and-forget manner; any exceptions are logged,
    and the function does not wait for completion or return a result.
    """
    logger.debug(f"Starting fire and forget task {task}")

    def run_async_task() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(task)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Exception in fire_and_forget task: {e}"
            )
        finally:
            loop.close()
            logger.debug(f"Task {task} finished")

    # Create and start a thread that will run the task in its own loop
    thread = threading.Thread(target=run_async_task)
    thread.daemon = True  # Thread will exit when main thread exits
    thread.start()
    logger.debug(f"Task started good bye :p {task}")
    return


async def best_within_timelimit(
    tasks: Sequence[Awaitable[T]],
    score_fn: Callable[[T, Awaitable[T]], float],
    timeout: float,
) -> T:
    """
    Runs a list of async tasks concurrently.
    Returns the best result (per score_fn) that completes before timeout.
    Ignores late results.

    Args:
        tasks: List of awaitable tasks that return results of type T
        score_fn: Function to score each result (higher is better), takes both the result and its awaitable
        timeout: Maximum time to wait (seconds)

    Returns:
        The best result according to score_fn, or None if no tasks complete in time
    """
    # Should not happen :)
    if not tasks:
        return None  # type: ignore[return-value]

    # Create task objects with Future results and wrap them
    original_to_task: Dict[asyncio.Task[T], Awaitable[T]] = {}
    wrapped_tasks: List[asyncio.Task[T]] = []

    for task in tasks:
        # Cast the awaitable to a coroutine to satisfy mypy
        coroutine: Coroutine[Any, Any, T] = cast(Coroutine[Any, Any, T], task)
        wrapped: asyncio.Task[T] = asyncio.create_task(coroutine)
        wrapped_tasks.append(wrapped)
        original_to_task[wrapped] = task

    # Wait for either all tasks or the timeout
    done, pending = await asyncio.wait(
        wrapped_tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED
    )

    # If nothing is done in the length of normal timeout we try again but short
    # circuit on first.
    if done is None or len(done) == 0:
        done, pending = await asyncio.wait(
            pending, timeout=timeout * 2, return_when=asyncio.FIRST_COMPLETED
        )

    asyncio.create_task(cancel_tasks(list(pending)))

    # Find the best result from completed tasks
    best_result: T
    best_score = float("-inf")  # Start with negative infinity for comparison

    for task in done:
        try:
            result = await task  # Get task result
            original_task = original_to_task[task]
            score = score_fn(result, original_task)
            if score > best_score:
                best_score = score
                best_result = result
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Task error in best_within_timelimit: {e}"
            )
            continue

    return best_result


async def best_within_timelimit_static(
    task_scores: Dict[Awaitable[T], float],
    timeout: float,
    extended_timeout: float = 600.0,
) -> T:
    """
    A simplified version of best_within_timelimit where scores are provided in advance.
    Returns early if the highest-scored task finishes before the time limit.
    If the highest-scored task doesn't finish in time, returns the best task completed so far,
    or if none have completed, waits for the next available task.

    Args:
        task_scores: Dictionary mapping awaitable tasks to their static scores
        timeout: Maximum time to wait for optimal result (seconds)
        extended_timeout: Additional time to wait for any result if needed (default: 10 minutes)

    Returns:
        - The result from a highest-scored task if one completes within timeout
        - Otherwise, the result from the best task that completed within timeout
        - If no tasks completed within the timeout, the result from the next task to complete
    """
    # Should not happen :)
    if not task_scores:
        raise ValueError("No tasks provided for best_within_timelimit_static")

    # Find all tasks with the maximum score
    max_score = max(task_scores.values())

    tasks: List[asyncio.Task[Tuple[T, float]]] = []

    # Create the wrapped tasks
    for task, score in task_scores.items():

        async def run_and_return_with_score(
            t: Awaitable[T], s: float
        ) -> Tuple[T, float]:
            result = await t
            return result, s

        tasks.append(asyncio.create_task(run_and_return_with_score(task, score)))

    best_result_so_far: Optional[T] = None
    best_score_so_far = float("-inf")

    # Wait for tasks to complete as they finish
    for future in asyncio.as_completed(tasks, timeout=timeout):
        try:
            result, score = await future

            # Track the best result seen so far
            if score > best_score_so_far:
                best_score_so_far = score
                best_result_so_far = result

            # If this is one of our best tasks, return immediately
            if score == max_score:
                # We don't explicitly cancel since cancel can be blocking depend on GC
                return result

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Task error in best_within_timelimit_static: {e}"
            )
            continue

    # If we got here, timeout occurred or all tasks finished but none of the best ones succeeded

    # Return the best result we've seen so far
    if best_result_so_far is not None:
        return best_result_so_far

    # Ok wait for any task to complete - don't filter for non-done tasks
    try:
        done, _ = await asyncio.wait(
            tasks, timeout=extended_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for completed_task in done:
            try:
                result, score = await completed_task

                # Track the best result seen so far
                if score > best_score_so_far:
                    best_score_so_far = score
                    best_result_so_far = result

            except Exception:
                continue
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error waiting for tasks after timeout: {e}"
        )

    # Return the best result we've found, or None if everything failed
    if best_result_so_far is not None:
        return best_result_so_far
    else:
        raise ValueError("No tasks completed successfully within the time limit.")


# Possible future TODO: Add a grace period after required to finish some optional tasks
async def execute_critical_optional_fireandforget(
    required: Sequence[Coroutine[Any, Any, T]],
    optional: Sequence[Coroutine[Any, Any, T]],
    fire_and_forget: Sequence[Coroutine] = [],
    done_record: Optional[T] = None,
    timeout: Optional[int] = None,
    max_extra_time_for_optional: int = 2,
) -> AsyncIterator[T]:
    """
    Runs required, optional, and fire-and-forget coroutines concurrently, yielding results as they complete.

    Starts all tasks at once: required tasks are awaited and must complete, optional tasks run concurrently and are canceled after required tasks finish or timeout, and fire-and-forget tasks are dispatched in background threads. Yields results from required and optional tasks as they finish. Optionally yields a final record when done.

    Args:
        required: Coroutines that must complete before optional tasks are canceled.
        optional: Coroutines that may be canceled if not finished after required tasks complete.
        fire_and_forget: Coroutines to run in background threads without blocking.
        done_record: Optional value to yield after all processing is complete.
        timeout: Maximum time in seconds to wait for required tasks before canceling optional tasks.
        max_extra_time_for_optional: Maximum additional seconds to wait for optional tasks after required tasks finish.

    Yields:
        Results from required and optional tasks as they complete, and optionally the done_record.
    """
    # Start fire and forget tasks
    logger.debug("Launching fire and forget")
    for fftask in fire_and_forget:
        await fire_and_forget_in_new_threadpool(fftask)
    logger.debug("Launched")

    # We create both sets of tasks at the same time since they're mostly independent and having
    # the optional ones running at the same time gives us a chance to get more done.
    required_tasks: List[asyncio.Task[T]] = [asyncio.create_task(t) for t in required]
    optional_tasks: List[asyncio.Task[T]] = [asyncio.create_task(t) for t in optional]
    all_tasks: List[asyncio.Task[T]] = required_tasks + optional_tasks

    required_set = set(required_tasks)
    required_tasks_finished = 0
    time_started = time.time()
    # First, execute required tasks (no timeout)
    try:
        for task in asyncio.as_completed(all_tasks, timeout=timeout):
            if task in required_set:
                required_tasks_finished += 1
            result: T = await task
            # Yield each result immediately for streaming
            yield result
            if required_tasks_finished >= len(required):
                logger.debug("All done with required tasks")
                break
    except asyncio.TimeoutError as e:
        logger.opt(exception=True).error(f"Timed out waiting for required tasks?")
    except Exception as e:
        logger.opt(exception=True).error(f"Error executing required tasks {e}")

    if timeout is None:
        logger.debug("No timeout set, so all tasks should be done")
        if done_record:
            yield done_record
        return
    try:
        time_core_finished = time.time()
        time_remaining_before_timeout: int = int(
            timeout - (time_core_finished - time_started) - 1
        )
        remaining_seconds: int = max(
            0,
            min(
                max_extra_time_for_optional,
                time_remaining_before_timeout,
            ),
        )
        logger.debug(
            f"Waiting for optional tasks to finish for {remaining_seconds} seconds"
        )
        # Wait for optional tasks to finish with a timeout
        for task in asyncio.as_completed(optional_tasks, timeout=remaining_seconds):
            optional_result: T = await task
            # Yield each result immediately for streaming
            yield optional_result
    except asyncio.TimeoutError as e:
        logger.debug(f"Timed out waiting for optional tasks?")
    except asyncio.exceptions.CancelledError:
        logger.debug("Cancellation waiting for optional tasks")
    finally:
        logger.debug(
            "Required tasks finished, fire and forget canceling optional tasks"
        )
        asyncio.create_task(cancel_tasks(optional_tasks))
        logger.debug("Optional tasks scheduled for cancelation")

    if done_record:
        yield done_record


def generate_random_filename_with_extension(original_filename: str) -> str:
    """Generate a random short filename with the same extension as the original."""
    _, ext = os.path.splitext(original_filename)
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return rand_str + ext


def generate_random_unsupported_filename() -> str:
    """Generate a random short filename with .unsupported extension."""
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return rand_str + ".unsupported"

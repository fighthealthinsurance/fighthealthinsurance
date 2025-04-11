from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings

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
    iterator: AsyncIterator[str], timeout: int = 45
) -> AsyncIterator[str]:
    """Interliave executor with some "" for keep alive.
    We add a "" ahead and behind along with every 45 seconds"""
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
        except StopAsyncIteration:
            # Break the loop if iteration is complete
            break


async def fire_and_forget_in_new_threadpool(task: Coroutine) -> None:
    """
    Runs an async task in a new threadpool executor.
    Fire-and-forget style with no return value.

    Args:
        task: The async task to run
    """
    logger.debug("Starting fire and forget task {task}")

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
            logger.debug("Task {task} finished")

    # Create and start a thread that will run the task in its own loop
    thread = threading.Thread(target=run_async_task)
    thread.daemon = True  # Thread will exit when main thread exits
    thread.start()
    logger.debug("Task started good bye :p")
    return


async def best_within_timelimit(
    tasks: Sequence[Awaitable[Optional[T]]],
    score_fn: Callable[[Optional[T], Awaitable[Optional[T]]], float],
    timeout: float,
) -> Optional[T]:
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
    if not tasks:
        return None

    # Create task objects with Future results and wrap them
    original_to_task: Dict[asyncio.Task[Optional[T]], Awaitable[Optional[T]]] = {}
    wrapped_tasks: List[asyncio.Task[Optional[T]]] = []

    for task in tasks:
        # Cast the awaitable to a coroutine to satisfy mypy
        coroutine: Coroutine[Any, Any, Optional[T]] = cast(
            Coroutine[Any, Any, Optional[T]], task
        )
        wrapped: asyncio.Task[Optional[T]] = asyncio.create_task(coroutine)
        wrapped_tasks.append(wrapped)
        original_to_task[wrapped] = task

    # Wait for either all tasks or the timeout
    done, pending = await asyncio.wait(
        wrapped_tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED
    )

    # Cancel any pending tasks
    for task in pending:
        task.cancel()

    # Find the best result from completed tasks
    best_result: Optional[T] = None
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
    task_scores: Dict[Awaitable[Optional[T]], float], timeout: float
) -> Optional[T]:
    """
    A simplified version of best_within_timelimit where scores are provided in advance.

    Args:
        task_scores: Dictionary mapping awaitable tasks to their static scores
        timeout: Maximum time to wait (seconds)

    Returns:
        The result from the highest-scored task that completes within the timeout
    """
    if not task_scores:
        return None

    # Extract tasks and create a static scoring function
    tasks: Sequence[Awaitable[Optional[T]]] = list(task_scores.keys())

    def static_score_fn(result: Optional[T], task: Awaitable[Optional[T]]) -> float:
        return task_scores.get(task, float("-inf"))

    # Delegate to the main implementation
    return await best_within_timelimit(tasks, static_score_fn, timeout)


# Possible future TODO: Add a grace period after required to finish some optional tasks
async def execute_critical_optional_fireandforget(
    required: Sequence[Coroutine[Any, Any, T]],
    optional: Sequence[Coroutine[Any, Any, T]],
    fire_and_forget: Sequence[Coroutine] = [],
    done_record: Optional[T] = None,
) -> AsyncIterator[T]:
    """
    Kicks off all tasks at once.
    Waits only for required tasks to finish; cancels optional after those finish.
    fire_and_forget tasks run in the background without blocking.

    Args:
        critical: Sequence of critical awaitable tasks that must complete
        optional: Sequence of optional awaitable tasks that may be canceled
        fire_and_forget: Sequence of awaitable tasks that should run in the background in another thread

    Returns:
        Async iterator of the values as finished
    """
    # Start fire and forget tasks
    for fftask in fire_and_forget:
        await fire_and_forget_in_new_threadpool(fftask)

    # We create both sets of tasks at the same time since they're mostly independent and having
    # the optional ones running at the same time gives us a chance to get more done.
    required_tasks: List[asyncio.Task[T]] = [asyncio.create_task(t) for t in required]
    optional_tasks: List[asyncio.Task[T]] = [asyncio.create_task(t) for t in optional]
    all_tasks: List[asyncio.Task[T]] = required_tasks + optional_tasks

    required_set = set(required_tasks)
    required_tasks_finished = 0
    # First, execute required tasks (no timeout)
    try:
        for task in asyncio.as_completed(all_tasks):
            if task in required_set:
                required_tasks_finished += 1
            result: T = await task
            # Yield each result immediately for streaming
            yield result
            if required_tasks_finished >= len(required):
                logger.debug("All done with required tasks")
                break
    finally:
        for t in optional_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*optional_tasks, return_exceptions=True)
    if done_record:
        yield done_record

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings

import asyncstdlib
import asyncio
from inspect import isabstract
import concurrent
import os
import re
from concurrent.futures import Future
from functools import reduce
from typing import AsyncIterator, Iterator, List, Optional, TypeVar
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

    try:
        # Render the plain text content
        text_content = render_to_string(
            f"emails/{template_name}.txt",
            context=context,
        )

        # Render the HTML content
        html_content = render_to_string(
            f"emails/{template_name}.html",
            context=context,
        )

        # Create the multipart email instance
        msg = EmailMultiAlternatives(
            subject,
            text_content,
            settings.EMAIL_HOST_USER,
            [to_email],
        )

        # Attach the HTML content and send the email
        msg.attach_alternative(html_content, "text/html")
        msg.send()

    except Exception as e:
        logger.exception("Failed to send email '%s' to %s", subject, to_email)
        raise


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


def is_valid_url(url) -> bool:
    try:
        result = requests.get(url)
        # If it we don't get a valid response try some quick cleanup.
        if result.status_code != 200:
            groups = maybe_bad_url_endings.search(url)
            if groups is not None:
                return is_valid_url(groups.group(1))
            else:
                return False
        result_text = result.text.lower()
        # Look for those craft 200 OKs which should be 404s
        for bad_result_text in common_bad_result:
            if bad_result_text.lower() in result_text:
                logger.debug(f"Found bad result on {url}")
                return False
        return True
    except RequestException as e:
        logger.debug(f"Error {e} looking up {url}")
        groups = maybe_bad_url_endings.search(url)
        if groups is not None:
            return is_valid_url(groups.group(1))
        else:
            return False


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

import asyncstdlib
import asyncio
from inspect import isabstract, isawaitable
import concurrent
import os
import re
from concurrent.futures import Future
from functools import reduce
from typing import AsyncIterator, Iterator, List, Optional, TypeVar
from uuid import UUID

import requests
from metapub import PubMedFetcher
from requests.exceptions import RequestException
from markdown_strings import esc_format

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


def markdown_escape(string: Optional[str]) -> str:
    if string is None:
        return ""
    result: str= esc_format(string, esc=True)
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
                print(f"Found bad result on {url}")
                return False
        return True
    except RequestException as e:
        print(f"Error {e} looking up {url}")
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


url_pattern = "https?:\\/\\/(?:www\\.)?[-a-zA-Z0-9@:%._\\+~#=]{1,256}\\.[a-zA-Z0-9()]{1,6}\\b(?:[-a-zA-Z0-9@:%_\\+.~#?&\\/=]*)"
url_re = re.compile(url_pattern, re.IGNORECASE)


def url_fixer(result: Optional[str]) -> Optional[str]:
    """LLMs like to hallucinate URLs drop them if they are not valid"""
    if result is None:
        return None
    else:
        urls = url_re.findall(result)
        for u in urls:
            print(f"{u}")
            if not is_valid_url(u):
                print(f"Removing invalud url {u}")
                result = result.replace(u, "")
        return result


def interleave_iterator_for_keep_alive(iterator: AsyncIterator[str]) -> AsyncIterator[str]:
    return asyncstdlib.iter(_interleave_iterator_for_keep_alive(iterator))


async def _interleave_iterator_for_keep_alive(iterator: AsyncIterator[str]) -> AsyncIterator[str]:
    yield ""
    await asyncio.sleep(0)
    async for item in iterator:
        await asyncio.sleep(0)
        yield ""
        await asyncio.sleep(0)
        yield item
        await asyncio.sleep(0)
        yield ""

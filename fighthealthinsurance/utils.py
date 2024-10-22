import os
import itertools
from uuid import UUID
from functools import reduce
import concurrent
from concurrent.futures import Future
from typing import List, Iterator, TypeVar

U = TypeVar("U")

flat_map = lambda f, xs: reduce(lambda a, b: a + b, map(f, xs))


def sekret_gen():
    return str(UUID(bytes=os.urandom(16), version=4))


def as_available(futures: List[Future[U]]) -> Iterator[U]:
    def complete(f: Future[U]) -> U:
        r = f.result()
        print(f"Returning {r}")
        return r

    return map(complete, concurrent.futures.as_completed(futures))

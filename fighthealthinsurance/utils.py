import os
from uuid import UUID
from functools import reduce

flat_map = lambda f, xs: reduce(lambda a, b: a + b, map(f, xs))


def sekret_gen():
    return str(UUID(bytes=os.urandom(16), version=4))

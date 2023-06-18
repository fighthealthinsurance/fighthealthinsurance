from functools import reduce

flat_map = lambda f, xs: reduce(lambda a, b: a + b, map(f, xs))

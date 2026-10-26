"""nginx's file locations must not swallow a page whose path only starts the same.

`location /media` is a prefix match, so it answered /media-references from disk
with nginx's own 404, and the press page never reached Django. A trailing slash
limits each location to its directory.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

CONF = Path(__file__).resolve().parents[2] / "conf" / "nginx.default"
# Plain prefix locations: no ~, ~*, = or ^~ modifier.
PREFIX = re.compile(r"^\s*location\s+(/[^\s{]*)\s*\{", re.M)


def _prefixes() -> list[str]:
    return [p for p in PREFIX.findall(CONF.read_text()) if p != "/"]


def _file_prefixes() -> list[str]:
    """The prefix locations that answer from disk (root or alias), not by proxy."""
    text = CONF.read_text()
    found = []
    for m in PREFIX.finditer(text):
        block = text[m.end() : text.index("}", m.end())]
        if re.search(r"^\s*(root|alias)\s", block, re.M) and m.group(1) != "/":
            found.append(m.group(1))
    return found


def _routes(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _routes(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern)


class NginxPrefixLocationsTest(SimpleTestCase):
    def test_file_locations_end_with_a_slash(self):
        self.assertEqual(sorted(_file_prefixes()), ["/media/", "/static/"])
        for prefix in _file_prefixes():
            with self.subTest(prefix=prefix):
                self.assertTrue(prefix.endswith("/"), prefix)

    def test_no_page_path_starts_with_a_file_location(self):
        for route in _routes(get_resolver().url_patterns):
            path = "/" + route.lstrip("^").lstrip("/")
            for prefix in _prefixes():
                with self.subTest(route=route, location=prefix):
                    self.assertFalse(
                        path.startswith(prefix),
                        f"{path} would be answered by nginx's {prefix} location",
                    )

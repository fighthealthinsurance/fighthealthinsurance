"""nginx caches only content-hashed compressor output for a year."""

import re
from pathlib import Path

from django.test import SimpleTestCase

CONF = Path(__file__).resolve().parents[2] / "conf" / "nginx.default"
LOCATION = re.compile(r'^\s*location\s+~\s+"([^"]+)"\s*\{([^}]*)\}', re.M)


def _immutable_locations() -> list[re.Pattern]:
    return [
        re.compile(m.group(1))
        for m in LOCATION.finditer(CONF.read_text())
        if "immutable" in m.group(2)
    ]


class HashedStaticCacheTest(SimpleTestCase):
    def test_one_immutable_location(self):
        self.assertEqual(len(_immutable_locations()), 1)

    def test_hashed_compressor_files_match(self):
        (pattern,) = _immutable_locations()
        for path in (
            "/static/CACHE/css/output.36830a7095de.css",
            "/static/CACHE/js/output.ff55a744d92a.js",
            "/static/CACHE/css/36830a7095de.css",
        ):
            with self.subTest(path=path):
                self.assertTrue(pattern.search(path))

    def test_unhashed_files_do_not_match(self):
        (pattern,) = _immutable_locations()
        for path in (
            "/static/CACHE/manifest.json",
            "/static/CACHE/css/main.css",
            "/static/CACHE/js/main.js",
            "/static/CACHE/css/output.36830a7095de.css.map",
            "/static/js/dist/main.js",
            "/static/images/logo.png",
            "/static/style.css",
        ):
            with self.subTest(path=path):
                self.assertFalse(pattern.search(path))

    def test_immutable_location_comes_after_the_deny_rules(self):
        text = CONF.read_text()
        cache_at = text.index("immutable")
        for deny in ("location ~ /node_modules/", "location ~ /\\."):
            with self.subTest(deny=deny):
                self.assertLess(text.index(deny), cache_at)

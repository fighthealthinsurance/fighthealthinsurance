"""Dist bundles and hand-written scripts are linked with a content version, so
a CDN or browser can't serve an old one beside a new page."""

import hashlib
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from fighthealthinsurance.templatetags import bundles

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "fighthealthinsurance/templates"
WEBPACK = REPO / "fighthealthinsurance/static/js/webpack.config.js"


class BundleTagTest(SimpleTestCase):
    def setUp(self):
        bundles._cached_version.cache_clear()
        self.addCleanup(bundles._cached_version.cache_clear)

    def render(self, name: str) -> str:
        return Template('{% load bundles %}{% bundle "' + name + '" %}').render(
            Context()
        )

    def bundle_file(self, contents: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=".bundle.js", delete=False)
        handle.write(contents)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_the_url_carries_the_contents_hash(self):
        with mock.patch.object(
            bundles.finders, "find", return_value=self.bundle_file(b"one")
        ):
            url = self.render("scrub")
        self.assertRegex(url, r"/static/js/dist/scrub\.bundle\.js\?v=[0-9a-f]{12}$")

    def test_new_contents_get_a_new_url(self):
        with override_settings(DEBUG=True):
            with mock.patch.object(
                bundles.finders, "find", return_value=self.bundle_file(b"one")
            ):
                first = self.render("scrub")
            with mock.patch.object(
                bundles.finders, "find", return_value=self.bundle_file(b"two")
            ):
                second = self.render("scrub")
        self.assertNotEqual(first, second)

    def test_a_bundle_gone_after_lookup_falls_back_to_the_plain_url(self):
        with mock.patch.object(
            bundles.finders, "find", return_value="/nonexistent/scrub.bundle.js"
        ):
            self.assertEqual(self.render("scrub"), "/static/js/dist/scrub.bundle.js")

    def test_a_missing_bundle_falls_back_to_the_plain_url(self):
        with mock.patch.object(bundles.finders, "find", return_value=None):
            self.assertEqual(
                self.render("nothing_here"), "/static/js/dist/nothing_here.bundle.js"
            )


class VersionedTagTest(SimpleTestCase):
    def setUp(self):
        bundles._cached_version.cache_clear()
        self.addCleanup(bundles._cached_version.cache_clear)

    def test_a_script_url_carries_the_contents_hash(self):
        url = Template("{% load bundles %}{% versioned 'js/denial_watch.js' %}").render(
            Context()
        )
        digest = hashlib.sha256(
            (REPO / "fighthealthinsurance/static/js/denial_watch.js").read_bytes()
        ).hexdigest()[:12]
        self.assertEqual(url, f"/static/js/denial_watch.js?v={digest}")

    def test_a_missing_file_falls_back_to_the_plain_url(self):
        url = Template("{% load bundles %}{% versioned 'js/not_here.js' %}").render(
            Context()
        )
        self.assertEqual(url, "/static/js/not_here.js")


def test_every_template_links_hand_written_scripts_through_the_tag():
    # Inside {% compress %} a script is folded into a file named by its contents already.
    compressed = re.compile(r"\{%\s*compress\b.*?\{%\s*endcompress\s*%\}", re.S)
    plain = re.compile(r"static ['\"]js/(?!dist/)[^'\"]+\.js['\"]")
    offenders = [
        str(p.relative_to(REPO))
        for folder in ("fighthealthinsurance", "fhi_users", "charts")
        for p in (REPO / folder / "templates").rglob("*.html")
        if plain.search(compressed.sub("", p.read_text()))
    ]
    assert offenders == []


def test_every_template_links_dist_bundles_through_the_tag():
    # entity_fetcher keeps its own ?frames= version, pinned by its tests.
    plain = re.compile(r"static ['\"]js/dist/(?!entity_fetcher)[^'\"]+\.bundle\.js")
    offenders = [
        str(p.relative_to(TEMPLATES))
        for p in TEMPLATES.rglob("*.html")
        if plain.search(p.read_text())
    ]
    assert offenders == []


def _bundles_templates_load() -> set[str]:
    tag = re.compile(r"\{%\s*bundle\s+['\"]([^'\"]+)['\"]")
    plain = re.compile(r"static ['\"]js/dist/([^'\"/]+)\.bundle\.js")
    names: set[str] = set()
    for folder in ("fighthealthinsurance", "charts", "fhi_users"):
        for path in (REPO / folder / "templates").rglob("*.html"):
            text = path.read_text()
            names.update(tag.findall(text), plain.findall(text))
    return names


def _webpack_entries() -> set[str]:
    listed = re.search(r"const entryFiles = \[(.*?)\];", WEBPACK.read_text(), re.S)
    assert listed, "webpack.config.js no longer lists its entries in entryFiles"
    return {Path(f).stem for f in re.findall(r"'([^']+\.tsx?)'", listed.group(1))}


def test_webpack_builds_exactly_the_bundles_pages_load():
    # A page bundle missing from the list 404s; an extra one ships unused.
    assert _webpack_entries() == _bundles_templates_load()

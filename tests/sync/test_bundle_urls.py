"""Dist bundles are linked with a content version, so a CDN can't serve an old one."""

import os
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from fighthealthinsurance.templatetags import bundles

TEMPLATES = Path(__file__).resolve().parents[2] / "fighthealthinsurance/templates"


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


def test_every_template_links_dist_bundles_through_the_tag():
    # entity_fetcher keeps its own ?frames= version, pinned by its tests.
    plain = re.compile(r"static ['\"]js/dist/(?!entity_fetcher)[^'\"]+\.bundle\.js")
    offenders = [
        str(p.relative_to(TEMPLATES))
        for p in TEMPLATES.rglob("*.html")
        if plain.search(p.read_text())
    ]
    assert offenders == []

"""collectstatic must not publish the JS build tree.

fighthealthinsurance/static/js is an app static dir, so AppDirectoriesFinder
collects everything under it. Django's default ignore_patterns are only
('CVS', '.*', '*~'), which meant node_modules -- ~1.4GB -- plus the TypeScript
sources and build config were copied into STATIC_ROOT, ADDed into the image by
k8s/Dockerfile, and served publicly by nginx at /static/js/... .

node_modules is gitignored, so it is invisible in the repo and present only at
build time; nothing in the test suite could observe it. These tests pin the
configuration instead: the shared ignore list keeps its load-bearing patterns,
and every collectstatic call site actually uses it. A call site that forgets
the flags is how this regressed originally.
"""

import re
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
IGNORES_SH = REPO_DIR / "scripts" / "collectstatic_ignores.sh"
NGINX_CONF = REPO_DIR / "conf" / "nginx.default"
CALL_SITES = [
    REPO_DIR / "scripts" / "build_static.sh",
    REPO_DIR / "scripts" / "setup_templates.sh",
]


class TestIgnoreList:
    def test_ignore_list_exists(self):
        assert IGNORES_SH.exists()

    @pytest.mark.parametrize(
        "pattern",
        ["node_modules", "*.ts", "*.tsx", "package.json", "webpack.config.js"],
    )
    def test_load_bearing_pattern_is_present(self, pattern):
        assert pattern in IGNORES_SH.read_text()

    def test_bundle_output_is_not_ignored(self):
        """js/dist/*.bundle.js must keep shipping -- ignoring it would take
        the whole frontend down, which is a worse outcome than the leak."""
        text = IGNORES_SH.read_text()
        for never in ["dist", "*.js'", "*.wasm", "*.mjs", "*.map"]:
            assert f"--ignore {never}" not in text

    def test_json_is_not_ignored_broadly(self):
        """state_help.json, microsites.json, blog_posts.json and
        denial_language.json are read at runtime via
        staticfiles_storage.open()."""
        assert "--ignore '*.json'" not in IGNORES_SH.read_text()

    def test_markdown_is_not_ignored(self):
        """views.py serves blog posts and FAQ entries by reading
        staticfiles_storage.path(f"blog/{slug}.md") / f"faq/{slug}.md" out of
        STATIC_ROOT at request time. Ignoring *.md 404s every blog and FAQ
        page -- caught only by a dry run, since no test renders them from a
        collected tree."""
        assert "--ignore '*.md'" not in IGNORES_SH.read_text()


class TestCallSitesUseTheList:
    @pytest.mark.parametrize("script", CALL_SITES, ids=lambda p: p.name)
    def test_every_collectstatic_call_passes_the_ignores(self, script):
        text = script.read_text()
        assert "collectstatic_ignores.sh" in text, f"{script.name} must source the list"
        for line in text.splitlines():
            if "manage.py collectstatic" in line and not line.strip().startswith("#"):
                assert "COLLECTSTATIC_IGNORES" in line, (
                    f"{script.name}: collectstatic call without the shared "
                    f"ignore list: {line.strip()}"
                )

    def test_no_unpatched_call_sites_elsewhere(self):
        """Catches a third script appearing without the flags."""
        for script in (REPO_DIR / "scripts").glob("*.sh"):
            for line in script.read_text().splitlines():
                if "manage.py collectstatic" in line and not line.strip().startswith("#"):
                    assert "COLLECTSTATIC_IGNORES" in line, (
                        f"{script.name} calls collectstatic without the shared "
                        f"ignore list: {line.strip()}"
                    )


class TestNginxDeniesTheTree:
    """setup_templates.sh runs `npm i` inside STATIC_ROOT after collecting, so
    the ignore list cannot remove node_modules on that path -- nginx does."""

    def test_node_modules_is_denied(self):
        assert re.search(
            r"location\s+~\s+/node_modules/\s*\{\s*deny all;", NGINX_CONF.read_text()
        )

    def test_node_modules_deny_is_slash_delimited(self):
        """Several webpack vendor bundles carry node_modules inside their
        FILENAME (vendors-node_modules_canvg_lib_index_es_js.bundle.js). A
        deny on the bare word would 403 them and take the frontend down, so
        the pattern must be path-delimited."""
        text = NGINX_CONF.read_text()
        assert "location ~ /node_modules/" in text
        assert not re.search(r"location\s+~\s+node_modules[^/]", text)

    def test_dotfiles_are_denied(self):
        assert re.search(r"location\s+~\s+/\\\.\s*\{\s*deny all;", NGINX_CONF.read_text())

    def test_well_known_is_exempt_and_ordered_first(self):
        """ACME/cert renewal breaks if .well-known is caught by the dotfile
        deny, and nginx takes the first matching regex location."""
        text = NGINX_CONF.read_text()
        well_known = text.index("location ~ ^/\\.well-known/")
        dotfile_deny = text.index("location ~ /\\.")
        assert well_known < dotfile_deny

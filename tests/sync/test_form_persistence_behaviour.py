"""The browser side of the health history page, run rather than read.

formPersistence.ts restores a local copy into an empty textarea on a GET. On a
page that carries the server's fingerprint of what is stored, that copy is
stale by definition: the history may have been removed from another device,
and the server would read the restored text as the person typing it back.
These run the compiled bundle in node against a fake DOM and localStorage,
through the bundle's own DOMContentLoaded initialisation.

The fake DOM is narrow on purpose: no real events, no cookies, a form
association supplied directly. A pass says the restore rule and the cleanup
behave; it says nothing about the rest of the page.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = REPO / "fighthealthinsurance/static/js/dist/formPersistence.bundle.js"
HARNESS = REPO / "tests/js/form_persistence_behaviour.cjs"


def _run(scenario: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    if not BUNDLE.exists():
        pytest.fail(
            f"{BUNDLE} is missing; CI builds it with scripts/ci_npm_build.sh, "
            "locally run npm run build in fighthealthinsurance/static/js"
        )
    done = subprocess.run(
        ["node", str(HARNESS), str(BUNDLE), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["initialised"], "the bundle registered no DOMContentLoaded handler"
    assert result["keysBefore"], "the harness seeded no local copy"
    return result


def test_a_plain_get_still_restores_the_local_copy():
    result = _run("plain-get")
    assert result["boxAfter"] == "old text from this browser"


def test_a_page_that_says_what_is_stored_is_not_overwritten_by_a_local_copy():
    result = _run("server-spoke")
    assert result["boxAfter"] == "", result
    assert not result["textLeftAnywhere"], "the stale copy must be cleared, not kept"


def test_the_cleanup_reaches_a_copy_the_getter_would_not_report():
    """A session-scoped wrapper holding "" answers the getter first; the bare
    key behind it still held the text, and used to survive."""
    result = _run("server-spoke-two-keys")
    assert result["boxAfter"] == "", result
    assert not result["textLeftAnywhere"], result

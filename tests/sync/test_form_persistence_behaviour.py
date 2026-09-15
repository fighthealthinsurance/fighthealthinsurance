"""The browser side of the health history page, run rather than read.

formPersistence.ts restores a local copy into an empty textarea on a GET. On a
page that carries the server's fingerprint of what is stored, that copy is
stale by definition: the history may have been removed from another device,
and the server would read the restored text as the person typing it back.
These run the compiled bundle in node against a fake DOM and localStorage.
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
            f"{BUNDLE} is missing; scripts/test_setup.sh builds it before the "
            "suite runs"
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
    assert result["hadCopy"], "the harness did not seed a local copy"
    return result


def test_a_plain_get_still_restores_the_local_copy():
    result = _run("plain-get")
    assert result["boxAfter"] == "old text from this browser"


def test_a_page_that_says_what_is_stored_is_not_overwritten_by_a_local_copy():
    result = _run("server-spoke")
    assert result["boxAfter"] == "", result
    assert result["copyAfter"] is None, "the stale copy must be cleared, not kept"

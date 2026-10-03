"""Text the intake page arrives with in the letter box stays there.

The server fills the letter box in three places: with the text it read from
an upload at /server_side_ocr, with the letter it sends back alongside an
error, and with a microsite's starting line. What this browser kept from an
earlier visit fills the box only when it is empty, so it never replaces that
text, and nothing on the way in empties the box.

There is no JS test harness in this repo, so this asserts on the source.
"""

import pathlib
import re

JS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
)


def _js_function(src: str, name: str) -> str:
    """The body of one JS/TS function, brace-matched."""
    start = src.index(name)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


def test_the_restore_on_load_only_fills_an_empty_field():
    src = (JS / "scrub_client_side_form.ts").read_text()
    restore = _js_function(src, "function retrieveFromLocalStorage")
    guard = restore.index('element.value === ""')
    assert guard < restore.index("getLocalStorageItemWithTTL(id)"), restore
    assert guard < restore.index("element.value = storedValue;"), restore
    # Nothing it does empties a field: a missing stored value leaves it be.
    assert not re.search(r'element\.value\s*=(?!=)[^;]*""', restore), restore


def test_that_restore_is_the_one_that_runs_on_load():
    """The guarded function above is what DOMContentLoaded calls, so the
    guard is on the path that runs when the page opens."""
    src = (JS / "scrub_client_side_form.ts").read_text()
    on_load = src[src.index('document.addEventListener("DOMContentLoaded"') :]
    assert "retrieveFromLocalStorage();" in on_load[: on_load.index("});")]


def test_the_letter_box_restore_in_the_page_setup_only_fills_an_empty_box():
    setup = _js_function((JS / "scrub.ts").read_text(), "function setupScrub")
    textareas = setup[setup.index("textareas.forEach") :]
    guard = textareas.index('if (textarea.value === "")')
    assert guard < textareas.index("textarea.value = storedValue"), textareas

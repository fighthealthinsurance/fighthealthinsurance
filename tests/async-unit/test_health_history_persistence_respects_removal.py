"""Clearing the health history box must not be undone by localStorage.

health_history.html tells the person "To remove it, clear the box and press
Next", and the server honours that. The browser then has its own copy:
formPersistence.ts saves the textarea on every keystroke and restores it on a
GET whenever the field renders EMPTY, which is exactly what a completed
removal looks like. The write key is also scoped to the denial_uuid in the
session only while base.html renders that meta tag, while the read falls back
to the unscoped key, so the two can disagree and a read can return the older
value.

So a submit with an empty box has to drop the saved copy, under both keys.

There is no JS test harness in this repo, so this asserts on the source.
"""

import pathlib
import re

SRC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
    / "formPersistence.ts"
)


def _block_from(src: str, start: int) -> str:
    """The brace-matched block opening at or after ``start``."""
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces from offset {start}")


def _js_function(src: str, name: str) -> str:
    """One function body. Matching starts at the LAST brace on the
    declaration line, because a parameter's inline type can carry braces of
    its own (``options?: { alwaysRestore?: boolean }``)."""
    start = src.index(name)
    line_end = src.index("\n", start)
    return _block_from(src, src.rindex("{", start, line_end))


def _submit_handler(body: str) -> str:
    m = re.search(r"addEventListener\(\s*['\"]submit['\"]", body)
    assert m, "no submit handler is registered"
    return _block_from(body, body.index("{", m.end()))


def test_an_empty_submit_drops_the_saved_copy():
    """The contract: on submit, an empty textarea clears rather than keeps."""
    handler = _submit_handler(
        _js_function(SRC.read_text(), "export function setupTextareaPersistence")
    )

    assert re.search(
        r"textarea\.value\s*===\s*(['\"])\1", handler
    ), f"the submit handler does not gate on an empty textarea: {handler}"
    assert (
        "clearLocalStorageItem(textareaId)" in handler
    ), f"the submit handler does not clear the persisted value: {handler}"


def test_the_clear_covers_both_keys_a_value_can_live_under():
    """getSessionScopedKey only scopes while the session meta tag is present
    and the read falls back to the unscoped key, so clearing one key leaves
    the other able to refill the box."""
    body = _js_function(SRC.read_text(), "export function clearLocalStorageItem")

    removed = [arg.strip() for arg in re.findall(r"removeItem\(([^)]*\)?)\)", body)]
    assert (
        "getSessionScopedKey(key)" in removed
    ), f"the session-scoped key is never removed: {removed}"
    assert "key" in removed, f"the unscoped fallback key is never removed: {removed}"


def test_the_restore_still_declines_when_the_box_already_has_a_value():
    """The guard the server-side render relies on: a box the server filled
    must never be overwritten by the browser's older copy."""
    body = _js_function(SRC.read_text(), "export function setupTextareaPersistence")

    should_restore = body[body.index("const shouldRestore") :]
    before_first_true = should_restore[: should_restore.index("return true")]
    assert re.search(
        r"textarea\.value\s*!==\s*(['\"])\1", before_first_true
    ), "shouldRestore no longer declines when the field is already filled"

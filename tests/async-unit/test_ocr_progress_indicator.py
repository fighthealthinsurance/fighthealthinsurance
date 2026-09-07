"""The "we're reading your file" box must appear while reading, not only on submit.

Reading one photographed page takes several seconds. The box existed, but the
only thing that ever showed it was the submit gate, so a user who waited quietly
saw an idle page and no explanation. It now shows when reading starts and hides
when the last read finishes.

There is no JS test harness in this repo, so this asserts on the source, matching
``test_scrub_ocr_quality.py``.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
JS = REPO / "static" / "js"
TEMPLATES = REPO / "templates"
CSS = REPO / "static" / "css"


def _form_source() -> str:
    return (JS / "scrub_client_side_form.ts").read_text()


def _js_function(src: str, name: str) -> str:
    """The body of one JS/TS function, brace-matched from the parameter list."""
    start = src.index(name)
    paren = src.index("(", start)
    depth = 0
    end_of_params = None
    for i in range(paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end_of_params = i
                break
    assert end_of_params is not None, f"unbalanced parens reading {name}"
    body_start = src.index("{", end_of_params)
    depth = 0
    for i in range(body_start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[body_start : i + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


def test_begin_ocr_shows_the_progress_box():
    """Show it when reading starts. This is the whole fix."""
    body = _js_function(_form_source(), "export function beginOcr")
    assert 'showHiddenMessage("ocr_in_progress")' in body, (
        "beginOcr no longer shows the progress box; the user is back to "
        "staring at an idle page while their file is read"
    )


def test_end_ocr_hides_the_progress_box_when_the_last_read_finishes():
    """And hide it after, or it outlives the work it describes."""
    body = _js_function(_form_source(), "export function endOcr")
    assert 'rehideHiddenMessage("ocr_in_progress")' in body
    assert "ocrInFlight === 0" in body, (
        "the hide is no longer guarded on the in-flight count, so one finished "
        "file would clear the message while other files are still being read"
    )


def test_progress_box_is_not_styled_as_an_error():
    """A normal wait must not look like a failure."""
    html = (TEMPLATES / "scrub.html").read_text()
    tag = re.search(r'<div[^>]*id="ocr_in_progress"[^>]*>', html)
    assert tag is not None, "ocr_in_progress box not found in scrub.html"
    assert "hidden-progress-message" in tag.group(0), (
        "the progress box lost its neutral styling class and inherits the red "
        "of .hidden-error-message again"
    )
    assert 'role="status"' in tag.group(0), (
        "progress is a status, not an alert; role=alert interrupts screen "
        "reader users for a routine wait"
    )
    css = (CSS / "custom.css").read_text()
    assert ".hidden-error-message.hidden-progress-message" in css, (
        "the neutral colour rule is gone, so the class in the template does "
        "nothing"
    )


def test_progress_copy_has_no_em_dash():
    """House style: no em dashes in user-facing copy."""
    html = (TEMPLATES / "scrub.html").read_text()
    start = html.index('id="ocr_in_progress"')
    block = html[start : html.index("</div>", start)]
    assert "&mdash;" not in block and "—" not in block, (
        "an em dash is back in the OCR progress copy"
    )

"""The client-side submit gate must require every field it validates.

`validateScrubForm` displayed a "need_denial" error and then submitted the
form anyway, because the gate condition only checked pii/privacy/email. The
server then rejected it with `denial_text: This field is required`, so the
user saw the page complain and submit at the same time. An in-flight OCR run
made it trivial to hit: a scanned PDF is rendered and OCR'd a page at a time,
and nothing stopped submission during that window.

There is no JS test harness in this repo, so this asserts on the source.
"""

import pathlib
import re

JS = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"


def _form_source() -> str:
    return (JS / "scrub_client_side_form.ts").read_text()


def _submit_gate(src: str) -> str:
    """The multi-field `if (...)` that decides whether the form submits.

    Anchored on denialTextReady so it cannot accidentally match one of the
    single-field `if (form.pii.checked)` validations earlier in the file.
    """
    m = re.search(
        r"if \([^{}]*form\.pii\.checked[^{}]*denialTextReady[^{}]*\)\s*\{", src, re.S
    )
    assert m, "could not find the submit gate condition"
    return m.group(0)


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


def test_denial_text_ready_is_derived_from_the_trimmed_field():
    """Assert the boolean CONTRACT, not that the expression appears
    somewhere in the file: a regression could define denialTextReady from
    something else entirely and a substring search would still pass
    (external review)."""
    src = _form_source()
    m = re.search(r"const\s+denialTextReady\s*=\s*([^;]+);", src)
    assert m, "denialTextReady is not assigned"
    assert m.group(1).strip() == "form.denial_text.value.trim().length > 0", m.group(1)


def test_submit_gate_ANDs_every_field_it_validates():
    """Every field validated above the gate must also gate it, combined with
    AND. Previously the gate checked only pii/privacy/email, so the form
    showed "need_denial" and submitted anyway."""
    gate = _submit_gate(_form_source())
    for field in ("pii", "privacy", "personalonly", "tos"):
        assert f"form.{field}.checked" in gate, f"{field} not gated: {gate}"
    assert "form.email.value.length > 0" in gate, gate
    assert "denialTextReady" in gate, gate
    # Combined with AND -- an OR would let any single field satisfy the gate.
    assert "||" not in gate, gate
    assert gate.count("&&") >= 5, gate


def test_whitespace_only_denial_text_is_treated_as_missing_everywhere():
    """The gate uses a trimmed predicate. If the validation branch does not,
    whitespace-only text blocks submission while showing no message at all --
    a silent refusal (external review)."""
    src = _form_source()
    assert "form.denial_text.value.trim().length < 1" in src
    # ...and no untrimmed length test on the field is left behind.
    assert not re.search(r"form\.denial_text\.value\.length\s*[<>]", src), src


def test_ocr_progress_message_is_cleared_when_the_last_run_finishes():
    """The gate only re-evaluates on submit, so endOcr must clear the message
    itself or it stays on screen after the file has been read."""
    end_ocr = _js_function(_form_source(), "export function endOcr")
    assert "ocrInFlight === 0" in end_ocr, end_ocr
    assert 'rehideHiddenMessage("ocr_in_progress")' in end_ocr, end_ocr


def test_the_uploader_releases_ocr_state_even_on_error():
    """Bind to the production path: recognizeEvent itself must wrap its
    recognize() loop and release in a finally, or a failed parse leaves the
    form permanently unsubmittable."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    assert "beginOcr();" in fn, fn
    assert "await recognize(" in fn, fn
    assert re.search(r"finally\s*\{\s*endOcr\(\);", fn), fn
    # ...and the release is inside the same function, after the loop.
    assert fn.index("beginOcr();") < fn.index("await recognize(") < fn.index("endOcr();")


def test_the_gate_actually_consults_ocr_state():
    src = _form_source()
    for fn in ("beginOcr", "endOcr", "isOcrInFlight"):
        assert f"export function {fn}" in src, fn
    validate = _js_function(src, "export function validateScrubForm")
    assert "isOcrInFlight()" in validate, validate
    assert 'showHiddenMessage("ocr_in_progress")' in validate, validate


def test_progress_message_element_exists():
    """The gate shows #ocr_in_progress; the template must define it or the
    user gets a silent refusal."""
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance" / "templates" / "scrub.html"
    ).read_text()
    assert 'id="ocr_in_progress"' in tpl

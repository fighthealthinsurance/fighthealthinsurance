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


def test_submit_gate_requires_denial_text():
    src = _form_source()
    gate = _submit_gate(src)
    body = gate
    assert "denialTextReady" in body, body
    # ...and denialTextReady is a non-empty check on the actual field.
    assert "form.denial_text.value.trim().length > 0" in src


def test_submit_gate_requires_every_checkbox_it_validates():
    """personalonly and tos were validated into agree_chk_error but never
    gated, so they could slip through the same way."""
    src = _form_source()
    gate = _submit_gate(src)
    for field in ("pii", "privacy", "personalonly", "tos"):
        assert f"form.{field}.checked" in gate, f"{field} not gated: {gate}"


def test_ocr_in_flight_is_tracked_and_released():
    """The uploader must mark OCR in flight and release it even on error, or
    a failed parse leaves the form permanently unsubmittable."""
    form_src = _form_source()
    for fn in ("beginOcr", "endOcr", "isOcrInFlight"):
        assert f"export function {fn}" in form_src, fn
    scrub_src = (JS / "scrub.ts").read_text()
    assert "beginOcr();" in scrub_src
    # endOcr must be in a finally, not merely after the await.
    assert re.search(r"finally\s*\{\s*endOcr\(\);", scrub_src), scrub_src


def test_progress_message_element_exists():
    """The gate shows #ocr_in_progress; the template must define it or the
    user gets a silent refusal."""
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance" / "templates" / "scrub.html"
    ).read_text()
    assert 'id="ocr_in_progress"' in tpl

"""Advanced OCR must ship OFF, on every surface, until the engine actually works.

The engine has never produced a character on prod: ``pipeline("image-to-text")``
resolves to ``AutoModelForVision2Seq``, whose registry does not contain
``qwen3_5``, so init throws ``Unsupported model type: qwen3_5`` and every caller
falls back to tesseract. Meanwhile the box was checked by default, so anyone
with WebGPU was offered a ~684 MB download for a feature that could not run.

These pin the off-by-default state in all three places it is decided, because
the default is one attribute and one boolean and both drift easily. There is no
JS test harness in this repo, so the client half asserts on the source, matching
``test_scrub_ocr_quality.py``.

Delete these only together with a measurement showing the engine beats tesseract
on real denial scans. See ``qwen_webgpu_ocr.ts`` for what a working version needs.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
JS = REPO / "static" / "js"
TEMPLATES = REPO / "templates"


def _scrub_template() -> str:
    return (TEMPLATES / "scrub.html").read_text()


def _ocr_source() -> str:
    return (JS / "scrub_ocr.ts").read_text()


def _qwen_source() -> str:
    return (JS / "qwen_webgpu_ocr.ts").read_text()


def _without_comments(src: str) -> str:
    """Source with // and /* */ comments removed.

    The comments in this module deliberately name the old broken pin so the next
    reader knows what not to reintroduce. Asserting against raw text would make
    explaining the bug indistinguishable from committing it.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", src)


def _checkbox_tag(html: str) -> str:
    """The advanced-OCR <input> tag, whole."""
    match = re.search(r"<input[^>]*id=\"advanced_ocr_enabled\"[^>]*>", html)
    assert match is not None, "advanced_ocr_enabled checkbox not found in scrub.html"
    return match.group(0)


def test_scrub_checkbox_markup_is_not_checked():
    """The rendered box is what a user sees before they touch anything."""
    tag = _checkbox_tag(_scrub_template())
    assert not re.search(r"\bchecked\b", tag), (
        "advanced OCR is checked by default again; the engine is still broken "
        f"and this offers a ~684 MB download for nothing. Tag: {tag}"
    )


def test_pages_without_the_checkbox_default_off():
    """explain_denial and chat_interface render no checkbox, so they cannot opt out.

    ``isAdvancedOCREnabled`` decides for them, and defaulting true there started
    the download on pages that never showed the user a choice.
    """
    src = _ocr_source()
    start = src.index("function isAdvancedOCREnabled")
    body = src[start : src.index("}", src.index("return", start))]
    assert "checkbox.checked : false" in body, (
        "isAdvancedOCREnabled no longer defaults to false when the checkbox is "
        "absent; the no-opt-out pages would start downloading the model again"
    )


def test_no_hardcoded_onnxruntime_version_pin():
    """The wasm path must be derived, never pinned.

    A hardcoded ``onnxruntime-web@1.22.0`` shipped for months while webpack
    bundled 1.24.2, so the runtime asked for ``asyncify`` files that 1.22.0's
    dist does not contain and the fetch 404'd. Any literal version pin here is
    the same bug waiting to happen, because the two versions drift
    independently.
    """
    code = _without_comments(_qwen_source())
    assert "wasmPaths" not in code, (
        "wasmPaths is being set again; let transformers.js derive it from the "
        "onnxruntime build it actually shipped with"
    )
    assert not re.search(
        r"onnxruntime-web@\d", code
    ), "a hardcoded onnxruntime-web version is back in qwen_webgpu_ocr.ts"


def test_label_states_the_download_cost():
    """Off-by-default is only honest if the label says what turning it on costs."""
    html = _scrub_template()
    section = html[html.index('id="advanced_ocr_section"') :][:1200]
    assert "684" in section, "the advanced OCR label no longer states the download size"
    assert (
        "huggingface.co" in section
    ), "the advanced OCR label no longer says where the model is downloaded from"

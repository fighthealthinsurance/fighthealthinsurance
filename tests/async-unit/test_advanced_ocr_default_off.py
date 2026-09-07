"""Advanced OCR must ship OFF, on every surface, until the engine actually works.

The engine has never produced a character on prod: ``pipeline("image-to-text")``
resolves to ``AutoModelForVision2Seq``, whose registry does not contain
``qwen3_5``, so init throws ``Unsupported model type: qwen3_5`` and every caller
falls back to tesseract. Meanwhile the box was checked by default, so anyone
with WebGPU was offered a ~684 MB download for a feature that could not run.

These pin the off-by-default state in every place it is decided, because the
default is one attribute and one boolean and both drift easily. There is no JS
test harness in this repo, so the client half asserts on the source, matching
``test_scrub_ocr_quality.py``. Comments are stripped before asserting, with
string literals preserved, so explaining the old bug in a comment is not the
same as reintroducing it, and a version pin hiding inside a URL string is still
caught (the first cut of these tests got both of those wrong).

Delete these only together with a measurement showing the engine beats tesseract
on real denial scans. See ``qwen_webgpu_ocr.ts`` for what a working version needs.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
JS = REPO / "static" / "js"
TEMPLATES = REPO / "templates"


def _strip_ts_comments(src: str) -> str:
    """TypeScript source with // and /* */ comments removed, strings kept.

    A regex that drops everything after ``//`` also drops the tail of
    ``"https://..."`` and so lets a versioned URL escape any assertion about
    it. This walks the source and skips over string literals (single, double,
    template) so a comment marker inside one is left alone. Regex literals
    containing ``//`` are not recognized and would be cut short; none of the
    files asserted on here contain one.
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ('"', "'", "`"):
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                j += 1
            out.append(src[i : j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_template_comments(html: str) -> str:
    """Django ``{# #}`` and HTML ``<!-- -->`` comments removed."""
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _js_function(src: str, name: str) -> str:
    """The body of one function, brace-matched from after its parameter list."""
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


def _scrub_template() -> str:
    return _strip_template_comments((TEMPLATES / "scrub.html").read_text())


def _ocr_code() -> str:
    return _strip_ts_comments((JS / "scrub_ocr.ts").read_text())


def _scrub_code() -> str:
    return _strip_ts_comments((JS / "scrub.ts").read_text())


def _qwen_code() -> str:
    return _strip_ts_comments((JS / "qwen_webgpu_ocr.ts").read_text())


def _checkbox_tag(html: str) -> str:
    """The live advanced-OCR <input> tag, whole. Case-insensitive: HTML is."""
    match = re.search(r"<input[^>]*id=\"advanced_ocr_enabled\"[^>]*>", html, re.I)
    assert match is not None, "advanced_ocr_enabled checkbox not found in scrub.html"
    return match.group(0)


def test_scrub_checkbox_markup_is_not_checked():
    """The rendered box is what a user sees before they touch anything."""
    tag = _checkbox_tag(_scrub_template())
    assert not re.search(r"\bchecked\b", tag, re.I), (
        "advanced OCR is checked by default again; the engine is still broken "
        f"and this offers a ~684 MB download for nothing. Tag: {tag}"
    )


def test_checkbox_init_never_turns_it_on():
    """scrub.ts may screen the box DOWN, never up.

    initAdvancedOCRCheckbox only ever sets checked=false. A single
    ``checkbox.checked = true`` there would restore the download for every
    WebGPU visitor while the markup test above still passed.
    """
    assert not re.search(r"\.checked\s*=\s*true\b", _scrub_code()), (
        "scrub.ts sets a checkbox to true; the advanced OCR box must never be "
        "turned on by initialization, only by the user"
    )


def test_pages_without_the_checkbox_default_off():
    """explain_denial and chat_interface render no checkbox, so they cannot opt out.

    ``isAdvancedOCREnabled`` decides for them, and defaulting true there started
    the download on pages that never showed the user a choice. Pin the exact
    return, and that it is the only one, so a second early ``return true`` above
    it is caught too.
    """
    body = _js_function(_ocr_code(), "function isAdvancedOCREnabled")
    returns = re.findall(r"\breturn\b[^;]*;", body)
    assert len(returns) == 1, f"expected exactly one return, found {returns}"
    assert re.fullmatch(
        r"return\s+checkbox\s*\?\s*checkbox\.checked\s*:\s*false\s*;", returns[0]
    ), (
        "isAdvancedOCREnabled no longer returns `checkbox ? checkbox.checked : "
        f"false`; the no-opt-out pages would start downloading again. Got: "
        f"{returns[0]}"
    )


def test_qwen_engine_only_runs_inside_the_checkbox_gate():
    """The gate is worthless if the engine is launched outside it."""
    body = _js_function(_ocr_code(), "async function recognizeImageText")
    assert body.count("recognizeWithQwenWebGPU(") == 1, (
        "recognizeWithQwenWebGPU is launched more than once, or not at all, in "
        "recognizeImageText"
    )
    assert re.search(
        r"if\s*\(\s*isAdvancedOCREnabled\(\)\s*\)\s*\{[^}]*recognizeWithQwenWebGPU\(",
        body,
        re.DOTALL,
    ), (
        "the qwen engine is no longer launched inside `if "
        "(isAdvancedOCREnabled())`; the checkbox would stop meaning anything"
    )


def test_no_hardcoded_onnxruntime_version_pin():
    """The wasm path must be derived, never pinned.

    A hardcoded ``onnxruntime-web@1.22.0`` shipped for months while webpack
    bundled 1.24.2, so the runtime asked for ``asyncify`` files that 1.22.0's
    dist does not contain and the fetch 404'd. Any literal version pin here is
    the same bug waiting to happen, because the two versions drift
    independently. Strings survive the comment strip, so a pin inside a URL
    literal is caught.
    """
    code = _qwen_code()
    assert "wasmPaths" not in code, (
        "wasmPaths is being set again; let transformers.js derive it from the "
        "onnxruntime build it actually shipped with"
    )
    assert not re.search(
        r"onnxruntime-web@\d", code
    ), "a hardcoded onnxruntime-web version is back in qwen_webgpu_ocr.ts"


def test_label_states_the_download_cost_truthfully():
    """Off-by-default is only honest if the label says what turning it on costs.

    And it must not overclaim the other way: standard OCR is not download-free,
    tesseract fetches its language data from a CDN on first use.
    """
    html = _scrub_template()
    start = html.index('id="advanced_ocr_section"')
    label = html[start : html.index("</label>", start)]
    assert "684" in label, "the advanced OCR label no longer states the download size"
    assert (
        "huggingface.co" in label
    ), "the advanced OCR label no longer says where the model is downloaded from"
    assert not re.search(r"needs\s+no\s+download", label, re.I), (
        "the label claims standard OCR needs no download; tesseract downloads "
        "its language file from a CDN on first use"
    )

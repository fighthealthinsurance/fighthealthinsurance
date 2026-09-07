"""Advanced OCR must ship OFF, on every surface, until the engine actually works.

The engine has never produced a character on prod: ``pipeline("image-to-text")``
resolves to ``AutoModelForVision2Seq``, whose registry does not contain
``qwen3_5``, so init throws ``Unsupported model type: qwen3_5`` and every caller
falls back to tesseract. Meanwhile the box was checked by default, so anyone
with WebGPU was offered a large download for a feature that could not run.

These pin the off-by-default state in every place it is decided, because the
default is one attribute and one boolean and both drift easily. There is no JS
test harness in this repo, so the client half asserts on the source, matching
``test_scrub_ocr_quality.py``.

Source is tokenized, not regexed, before asserting. Two views come out of it:
one with comments removed and string literals kept, for "no version pin
anywhere in executable code" (a pin inside a URL literal must still be caught);
and one with string literals blanked as well, for every structural check, so a
string containing ``if (isAdvancedOCREnabled()) {`` or a stray ``}`` cannot
impersonate syntax. Both earlier cuts of this file were beaten exactly those
ways in review.

Delete these only together with a measurement showing the engine beats tesseract
on real denial scans. See ``qwen_webgpu_ocr.ts`` for what a working version needs.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
JS = REPO / "static" / "js"
TEMPLATES = REPO / "templates"


def _tokenize_ts(src: str):
    """Split TypeScript into ('code' | 'string' | 'comment', text) segments.

    A template literal is one 'string' token INCLUDING its ``${...}``
    interpolations, which are scanned recursively so a nested template or a
    brace inside one cannot end the outer literal early. Regex literals are not
    recognized; none of the files asserted on contain one with ``//`` in it.
    """
    n = len(src)

    def string_end(i: int, quote: str) -> int:
        j = i + 1
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == quote:
                return j + 1
            if quote == "`" and src.startswith("${", j):
                j = interpolation_end(j + 2)
                continue
            j += 1
        return n

    def interpolation_end(i: int) -> int:
        """Index just past the ``}`` closing the ``${`` opened before ``i``."""
        depth = 0
        j = i
        while j < n:
            c = src[j]
            if c in "\"'`":
                j = string_end(j, c)
                continue
            if src.startswith("//", j):
                k = src.find("\n", j)
                j = n if k < 0 else k
                continue
            if src.startswith("/*", j):
                k = src.find("*/", j + 2)
                j = n if k < 0 else k + 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return j + 1
                depth -= 1
            j += 1
        return n

    tokens = []
    buf = []
    i = 0

    def flush():
        if buf:
            tokens.append(("code", "".join(buf)))
            buf.clear()

    while i < n:
        c = src[i]
        if c in "\"'`":
            flush()
            j = string_end(i, c)
            tokens.append(("string", src[i:j]))
            i = j
        elif src.startswith("//", i):
            flush()
            k = src.find("\n", i)
            j = n if k < 0 else k
            tokens.append(("comment", src[i:j]))
            i = j
        elif src.startswith("/*", i):
            flush()
            k = src.find("*/", i + 2)
            j = n if k < 0 else k + 2
            tokens.append(("comment", src[i:j]))
            i = j
        else:
            buf.append(c)
            i += 1
    flush()
    return tokens


def _executable(src: str) -> str:
    """Comments removed, string literals kept verbatim."""
    return "".join(t for k, t in _tokenize_ts(src) if k != "comment")


def _structural(src: str) -> str:
    """Comments removed AND every string literal replaced by an empty one."""
    return "".join(
        t if k == "code" else ('""' if k == "string" else "")
        for k, t in _tokenize_ts(src)
    )


def _strip_template_comments(html: str) -> str:
    """Django ``{# #}`` and HTML ``<!-- -->`` comments removed."""
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _brace_block(src: str, open_at: int) -> str:
    """``src[open_at:]`` up to and including the brace that closes ``open_at``."""
    assert src[open_at] == "{", src[open_at : open_at + 20]
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def _js_function(src: str, declaration: str) -> str:
    """Body of the function declared EXACTLY as ``declaration`` followed by ``(``.

    ``function isAdvancedOCREnabled`` must not match
    ``function isAdvancedOCREnabledForCheckbox``; requiring the parameter list
    to open right after the name is what stops a look-alike helper from being
    inspected in place of the real thing.
    """
    match = re.search(re.escape(declaration) + r"\s*\(", src)
    assert match is not None, f"{declaration}( not found"
    depth = 0
    end_of_params = None
    for i in range(match.end() - 1, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end_of_params = i
                break
    assert end_of_params is not None, f"unbalanced parens reading {declaration}"
    return _brace_block(src, src.index("{", end_of_params))


def _scrub_template() -> str:
    return _strip_template_comments((TEMPLATES / "scrub.html").read_text())


def _ocr_structural() -> str:
    return _structural((JS / "scrub_ocr.ts").read_text())


def _scrub_structural() -> str:
    return _structural((JS / "scrub.ts").read_text())


def _qwen_executable() -> str:
    return _executable((JS / "qwen_webgpu_ocr.ts").read_text())


def _checkbox_tag(html: str) -> str:
    """The live advanced-OCR <input> tag, whole. Case-insensitive: HTML is."""
    match = re.search(r"<input[^>]*id=\"advanced_ocr_enabled\"[^>]*>", html, re.I)
    assert match is not None, "advanced_ocr_enabled checkbox not found in scrub.html"
    return match.group(0)


def test_tokenizer_keeps_a_pin_inside_a_nested_template_literal():
    """Guard the guard: the exact construct that beat the previous stripper."""
    sample = "const u = `${`https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/`}`; // x\n"
    assert "onnxruntime-web@1.22.0" in _executable(sample)
    assert "onnxruntime-web@1.22.0" not in _structural(sample)
    assert "// x" not in _executable(sample)


def test_scrub_checkbox_markup_is_not_checked():
    """The rendered box is what a user sees before they touch anything."""
    tag = _checkbox_tag(_scrub_template())
    assert not re.search(r"\bchecked\b", tag, re.I), (
        "advanced OCR is checked by default again; the engine is still broken "
        f"and this offers a large download for nothing. Tag: {tag}"
    )


def test_checkbox_init_never_turns_it_on():
    """scrub.ts may screen the box DOWN, never up.

    initAdvancedOCRCheckbox only ever sets checked=false. A single
    ``checkbox.checked = true`` there would restore the download for every
    WebGPU visitor while the markup test above still passed.
    """
    assert not re.search(r"\.checked\s*=\s*true\b", _scrub_structural()), (
        "scrub.ts sets a checkbox to true; the advanced OCR box must never be "
        "turned on by initialization, only by the user"
    )


def test_pages_without_the_checkbox_default_off():
    """explain_denial and chat_interface render no checkbox, so they cannot opt out.

    ``isAdvancedOCREnabled`` decides for them, and defaulting true there started
    the download on pages that never showed the user a choice. Pin the exact
    return, and that it is the only one, so a second early ``return true`` above
    it is caught too. Strings are blanked first, so a string cannot supply the
    expected text.
    """
    body = _js_function(_ocr_structural(), "function isAdvancedOCREnabled")
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
    """The gate is worthless if the engine is launched outside it.

    Brace-match the ``if (isAdvancedOCREnabled())`` block on string-blanked
    source and require the one launch to sit inside it. A string containing the
    gate's text, or a ``}`` inside a string, cannot fake a gate here.
    """
    body = _js_function(_ocr_structural(), "async function recognizeImageText")
    assert body.count("recognizeWithQwenWebGPU(") == 1, (
        "recognizeWithQwenWebGPU is launched more than once, or not at all, in "
        "recognizeImageText"
    )
    gate = re.search(r"if\s*\(\s*isAdvancedOCREnabled\(\)\s*\)\s*\{", body)
    assert gate is not None, "no `if (isAdvancedOCREnabled())` block in recognizeImageText"
    block = _brace_block(body, gate.end() - 1)
    assert "recognizeWithQwenWebGPU(" in block, (
        "the qwen engine is launched outside the `if (isAdvancedOCREnabled())` "
        "block; the checkbox would stop meaning anything"
    )


def test_no_hardcoded_onnxruntime_version_pin():
    """The wasm path must be derived, never pinned.

    A hardcoded ``onnxruntime-web@1.22.0`` shipped for months while webpack
    bundled 1.24.2, so the runtime asked for ``asyncify`` files that 1.22.0's
    dist does not contain and the fetch 404'd. Any literal version pin here is
    the same bug waiting to happen, because the two versions drift
    independently. Strings, nested templates included, are kept in this view.
    """
    code = _qwen_executable()
    assert "wasmPaths" not in code, (
        "wasmPaths is being set again; let transformers.js derive it from the "
        "onnxruntime build it actually shipped with"
    )
    assert not re.search(
        r"onnxruntime-web@\d", code
    ), "a hardcoded onnxruntime-web version is back in qwen_webgpu_ocr.ts"


def test_label_states_the_download_cost_truthfully():
    """Off-by-default is only honest if the label tells the truth in both directions.

    It must state the size in MB (not just the digits) and the source. It must
    not claim standard OCR needs no download or no model: tesseract fetches
    English trained data, which is an LSTM model, from a CDN on first use.
    """
    html = _scrub_template()
    start = html.index('id="advanced_ocr_section"')
    label = html[start : html.index("</label>", start)]
    assert re.search(r"\b684\s*MB\b", label), (
        "the advanced OCR label no longer states the download size in MB"
    )
    assert (
        "huggingface.co" in label
    ), "the advanced OCR label no longer says where the model is downloaded from"
    assert not re.search(r"needs\s+no\s+download", label, re.I), (
        "the label claims standard OCR needs no download; tesseract downloads "
        "its language file from a CDN on first use"
    )
    assert not re.search(r"\bno\s+model\b", label, re.I), (
        "the label claims standard OCR uses no model; tesseract's trained data "
        "is an LSTM model"
    )

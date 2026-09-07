"""Runtime worker assets must never be loaded from a node_modules URL.

pdf.js and tesseract.js load their workers by URL at runtime. They used to
point at /static/js/node_modules/..., which PR #936 correctly stopped
publishing (collectstatic ignore + nginx `deny all`) -- and that silently
broke PDF loading and OCR in production. The webpack build now copies the
assets into dist/workers/; these tests pin both halves so the regression
cannot recur unnoticed.
"""

import re
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent.parent / "fighthealthinsurance" / "static" / "js"
WEBPACK = JS_DIR / "webpack.config.js"
SOURCES = [
    p
    for p in list(JS_DIR.glob("*.ts")) + list(JS_DIR.glob("*.tsx"))
    if "node_modules" not in p.parts
]


def test_no_runtime_source_references_a_node_modules_url():
    offenders = [
        p.name
        for p in SOURCES
        if re.search(r"static/js/node_modules|node_module_path", p.read_text())
    ]
    assert offenders == [], f"node_modules URLs in: {offenders}"


def test_webpack_copies_every_runtime_worker_asset_into_dist_workers():
    text = WEBPACK.read_text()
    assert "copy-webpack-plugin" in text
    for src, dest in (
        ("pdfjs-dist/build/pdf.worker.min.mjs", "workers/pdf.worker.min.js"),
        ("tesseract.js/dist/worker.min.js", "workers/tesseract.js/worker.min.js"),
        ("tesseract.js-core/", "workers/tesseract.js-core/"),
    ):
        assert src in text, f"missing copy source {src}"
        assert dest in text, f"missing copy destination {dest}"


def test_sources_point_at_the_copied_workers():
    shared = (JS_DIR / "shared.ts").read_text()
    ocr = (JS_DIR / "scrub_ocr.ts").read_text()
    assert '"/static/js/dist/workers/"' in shared
    assert 'workers_path + "pdf.worker.min.js"' in shared
    assert 'workers_path + "tesseract.js-core"' in ocr
    assert 'workers_path + "tesseract.js/worker.min.js"' in ocr


def test_no_runtime_worker_is_published_or_loaded_with_an_mjs_extension():
    """The web image's nginx has no MIME entry for .mjs.

    It served dist/workers/pdf.worker.min.mjs as application/octet-stream,
    browsers refuse to run a module worker (or dynamic import()) without a
    JavaScript MIME type, and pdf.js's fake-worker fallback does the same
    import -- so every PDF attached on /scan failed with "Setting up fake
    worker failed" while images read fine. Publishing the same bytes under
    .js is what fixed it; keep it that way.
    """
    webpack = WEBPACK.read_text()
    published_as_mjs = re.findall(r"to:\s*'workers/[^']*\.mjs'", webpack)
    assert published_as_mjs == [], published_as_mjs

    loaded_as_mjs = {
        p.name: re.findall(r'workers_path\s*\+\s*"[^"]*\.mjs"', p.read_text())
        for p in SOURCES
    }
    loaded_as_mjs = {k: v for k, v in loaded_as_mjs.items() if v}
    assert loaded_as_mjs == {}, loaded_as_mjs

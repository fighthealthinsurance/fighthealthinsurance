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
        ("pdfjs-dist/build/pdf.worker.min.mjs", "workers/pdf.worker.min.mjs"),
        ("tesseract.js/dist/worker.min.js", "workers/tesseract.js/worker.min.js"),
        ("tesseract.js-core/", "workers/tesseract.js-core/"),
    ):
        assert src in text, f"missing copy source {src}"
        assert dest in text, f"missing copy destination {dest}"


def test_sources_point_at_the_copied_workers():
    shared = (JS_DIR / "shared.ts").read_text()
    ocr = (JS_DIR / "scrub_ocr.ts").read_text()
    assert '"/static/js/dist/workers/"' in shared
    assert 'workers_path + "pdf.worker.min.mjs"' in shared
    assert 'workers_path + "tesseract.js-core"' in ocr
    assert 'workers_path + "tesseract.js/worker.min.js"' in ocr

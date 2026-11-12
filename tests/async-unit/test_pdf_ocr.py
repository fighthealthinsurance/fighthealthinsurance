"""Reading scanned PDF pages with tesseract, in a child process, through
pipes only, within a page budget and the server OCR's pixel cap."""

import asyncio
import os
import stat
import struct
import sys
import tempfile
import time

import pymupdf
import pytest

from fighthealthinsurance import pdf_text
from tests.conftest import skip_if_no_tesseract
from tests.pdf_fixtures import make_pdf_bytes, make_scanned_pdf_bytes
from tests.plan_text_samples import PLAN_TEXT

#: Stands in for tesseract: says what it was given on stdin.
SAYS_WHAT_IT_GOT = (
    "import struct, sys; d = sys.stdin.buffer.read(); "
    "w, h = struct.unpack('>II', d[16:24]); "
    "print(f'png {w} {h}' if d[:8] == b'\\x89PNG\\r\\n\\x1a\\n' else 'not a png')"
)
TAKES_ITS_TIME = "import time; time.sleep(5); print('late')"
WRITES_A_LOT = "print('x' * 1000)"
#: Pixels in a page of make_pdf_bytes (PyMuPDF's default, A4) drawn at 200 dpi.
A4_AT_200_DPI = (595 / 72 * 200) * (842 / 72 * 200)


def ocr(content, pages, monkeypatch, script=SAYS_WHAT_IT_GOT, **kw):
    monkeypatch.setattr(pdf_text, "_TESSERACT", (sys.executable, "-c", script))
    args = dict(max_chars=100_000, dpi=200, max_pixels=50_000_000,
                seconds_per_page=30, budget_seconds=120)
    args.update(kw)
    return pdf_text.ocr_pdf_pages(content, pages, **args)


def test_each_page_goes_to_tesseract_as_a_png_on_stdin(monkeypatch):
    got = ocr(make_pdf_bytes(["", "", ""]), [0, 2], monkeypatch)
    assert [number for number, _ in got["texts"]] == [0, 2]
    assert all(text.startswith("png ") for _, text in got["texts"])
    assert got["stopped_at"] is None


def test_a_page_is_drawn_at_the_dpi_asked_for(monkeypatch):
    (_, text), = ocr(make_pdf_bytes([""]), [0], monkeypatch)["texts"]
    _, width, height = text.split()
    assert int(width) * int(height) == pytest.approx(A4_AT_200_DPI, rel=0.01)


def test_a_page_is_drawn_smaller_to_stay_within_the_pixel_cap(monkeypatch):
    (_, text), = ocr(make_pdf_bytes([""]), [0], monkeypatch, max_pixels=1_000_000)["texts"]
    _, width, height = text.split()
    assert int(width) * int(height) <= 1_000_000


def test_a_page_that_cannot_fit_the_pixel_cap_is_never_drawn(monkeypatch):
    def drawn(*args, **kwargs):
        raise AssertionError("the page was drawn")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", drawn)
    got = ocr(make_pdf_bytes([""]), [0], monkeypatch, max_pixels=10_000)
    assert got["texts"] == [[0, None]]


def test_a_page_tesseract_fails_on_reads_as_none(monkeypatch):
    got = ocr(make_pdf_bytes([""]), [0], monkeypatch, script="import sys; sys.exit(1)")
    assert got["texts"] == [[0, None]]


def test_a_page_tesseract_reads_with_nothing_on_it_reads_as_empty(monkeypatch):
    got = ocr(make_pdf_bytes([""]), [0], monkeypatch, script="pass")
    assert got["texts"] == [[0, ""]]


def test_a_page_tesseract_does_not_finish_reads_as_none(monkeypatch):
    got = ocr(make_pdf_bytes([""]), [0], monkeypatch, script=TAKES_ITS_TIME, seconds_per_page=0.5)
    assert got == {"texts": [[0, None]], "stopped_at": None}


def test_reading_stops_when_the_budget_is_used_up(monkeypatch):
    got = ocr(make_pdf_bytes(["", ""]), [0, 1], monkeypatch, budget_seconds=1)
    assert got == {"texts": [], "stopped_at": 0}


def test_reading_stops_once_max_chars_are_collected(monkeypatch):
    got = ocr(make_pdf_bytes(["", ""]), [0, 1], monkeypatch, script=WRITES_A_LOT, max_chars=10)
    assert got == {"texts": [[0, "x" * 10]], "stopped_at": 1}


def test_tesseract_gets_none_of_the_workers_secrets(monkeypatch):
    monkeypatch.setenv("DEFF_PASSWORD", "made-up-secret")
    script = "import os; print('leaked' if 'DEFF_PASSWORD' in os.environ else 'clean')"
    got = ocr(make_pdf_bytes([""]), [0], monkeypatch, script=script)
    assert got["texts"] == [[0, "clean\n"]]


def test_without_tesseract_it_says_so(monkeypatch):
    monkeypatch.setattr(pdf_text, "_TESSERACT", ("/nonexistent/tesseract-for-tests",))
    with pytest.raises(ValueError, match="tesseract is not installed"):
        pdf_text.ocr_pdf_pages(make_pdf_bytes([""]), [0], 100, 200, 50_000_000, 30, 120)


@pytest.mark.asyncio
async def test_a_pdf_that_will_not_open_is_reported_by_the_child():
    with pytest.raises(ValueError):
        await pdf_text.aocr_pdf_pages(b"%PDF-1.7 not a PDF after all", [0], 100, 200, 50_000_000, 30, 60)


@skip_if_no_tesseract
@pytest.mark.asyncio
async def test_a_scanned_page_is_read_and_nothing_is_written_to_disk():
    scratch = tempfile.gettempdir()
    before = set(os.listdir(scratch))
    got = await pdf_text.aocr_pdf_pages(
        make_scanned_pdf_bytes([PLAN_TEXT]), [0], 100_000, 200, 50_000_000, 60, 120
    )
    (number, text), = got["texts"]
    assert number == 0 and "deductible" in text.lower() and "network" in text.lower()
    assert set(os.listdir(scratch)) <= before


def _state(pid):
    """A process's state letter, or None once it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.asyncio
async def test_a_tesseract_still_running_at_the_deadline_is_killed_with_the_child(
    tmp_path, monkeypatch
):
    # A stand-in tesseract on PATH that says its pid and then hangs.
    pid_file = tmp_path / "pid"
    fake = tmp_path / "tesseract"
    fake.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    # The parent's deadline before the child's own page timeout.
    monkeypatch.setattr(pdf_text, "_OCR_GRACE_SECONDS", -50)
    with pytest.raises(TimeoutError):
        await pdf_text.aocr_pdf_pages(make_pdf_bytes([""]), [0], 100, 200, 50_000_000, 60, 60)
    pid = int(pid_file.read_text())
    for _ in range(50):
        if _state(pid) in (None, "Z"):
            break
        await asyncio.sleep(0.1)
    assert _state(pid) in (None, "Z")

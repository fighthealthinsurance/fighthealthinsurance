"""Reading and merging fetched PDFs with pypdf, in a child process, under a timeout."""

import asyncio
import io
import json
import resource
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

import pymupdf
import pytest
from pypdf import PdfReader

from fighthealthinsurance import pdf_text
from tests.pdf_fixtures import (
    make_pdf_bytes,
    make_shared_long_string_pdf_bytes,
    make_shared_stream_pdf_bytes,
    make_table_pdf_bytes,
)

#: The child caps its address space with RLIMIT_AS only on Linux.
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="RLIMIT_AS is only enforced on Linux"
)


class TestExtractPdfPageTexts:
    def test_reads_each_page_in_order(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])

        texts = pdf_text.extract_pdf_page_texts(pdf, max_pages=10, max_chars=10_000)

        assert len(texts) == 2
        assert "First page words" in texts[0]
        assert "Second page words" in texts[1]

    def test_stops_reading_at_the_page_cap(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        texts = pdf_text.extract_pdf_page_texts(pdf, max_pages=2, max_chars=10_000)

        assert len(texts) == 2
        assert not any("Page three" in text for text in texts)

    def test_stops_reading_once_max_chars_are_collected(self):
        pdf = make_pdf_bytes(["First page words", "Second page words", "Third"])
        first, second, _ = pdf_text.extract_pdf_page_texts(
            pdf, max_pages=10, max_chars=10_000
        )

        texts = pdf_text.extract_pdf_page_texts(
            pdf, max_pages=10, max_chars=len(first) + 6
        )

        assert texts == [first, second[:6]]

    def test_opens_a_pdf_encrypted_with_an_empty_password(self):
        pdf = make_pdf_bytes(["Readable without a password"], user_password="")

        texts = pdf_text.extract_pdf_page_texts(pdf, max_pages=10, max_chars=10_000)

        assert "Readable without a password" in texts[0]

    def test_pdf_that_needs_a_password_raises_value_error(self):
        pdf = make_pdf_bytes(["Locked words"], user_password="open-sesame")

        with pytest.raises(ValueError, match="cannot be decrypted"):
            pdf_text.extract_pdf_page_texts(pdf, max_pages=10, max_chars=10_000)


class TestMupdfPages:
    def test_reads_each_page_in_order(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])

        pages = pdf_text.mupdf_pages(pdf, max_pages=10, max_chars=10_000, tables=False)

        assert [("First" in p["text"], "Second" in p["text"]) for p in pages] == [
            (True, False),
            (False, True),
        ]
        assert [p["tables"] for p in pages] == [[], []]

    def test_stops_reading_at_the_page_cap(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        pages = pdf_text.mupdf_pages(pdf, max_pages=2, max_chars=10_000, tables=False)

        assert len(pages) == 2

    def test_stops_reading_once_max_chars_are_collected(self):
        pdf = make_pdf_bytes(["First page words", "Second page words", "Third"])
        first, second, _ = [
            p["text"]
            for p in pdf_text.mupdf_pages(pdf, max_pages=10, max_chars=10_000, tables=False)
        ]

        pages = pdf_text.mupdf_pages(
            pdf, max_pages=10, max_chars=len(first) + 6, tables=False
        )

        assert [p["text"] for p in pages] == [first, second[:6]]

    def test_finds_tables_when_asked(self):
        rows = [["CPT Code", "Description"], ["72148", "MRI lumbar spine"]]
        pdf = make_table_pdf_bytes(rows)

        (page,) = pdf_text.mupdf_pages(pdf, max_pages=10, max_chars=10_000, tables=True)

        assert page["tables"] == [rows]

    def test_table_cells_count_against_the_cap(self):
        rows = [["CPT Code", "Description"], ["72148", "MRI lumbar spine"]]
        pdf = make_table_pdf_bytes(rows)
        # The table, then the header row and its two cells, and no more.
        header = 1 + sum(len(cell) + 1 for cell in rows[0])

        (page,) = pdf_text.mupdf_pages(pdf, max_pages=10, max_chars=1 + header, tables=True)

        assert page == {"text": "", "tables": [[rows[0]]]}

    def test_pages_past_the_page_cap_are_never_loaded(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        with loads_failing_from(2) as loaded:
            pages = pdf_text.mupdf_pages(pdf, max_pages=2, max_chars=10_000, tables=False)

        assert loaded == [0, 1]
        assert "Page two" in pages[1]["text"]

    def test_pages_past_the_character_cap_are_never_loaded(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])
        (first, _) = pdf_text.mupdf_pages(pdf, max_pages=10, max_chars=10_000, tables=False)

        with loads_failing_from(1) as loaded:
            pages = pdf_text.mupdf_pages(
                pdf, max_pages=10, max_chars=len(first["text"]), tables=False
            )

        assert loaded == [0]
        assert pages == [first]

    def test_text_is_not_read_once_tables_use_up_the_cap(self):
        rows = [["CPT Code", "Description"], ["72148", "MRI lumbar spine"]]
        pdf = make_table_pdf_bytes(rows, text="Prior authorization list")
        header = 1 + sum(len(cell) + 1 for cell in rows[0])
        real_get_text = pymupdf.Page.get_text
        calls = []

        # The table finder reads words and blocks itself; count the text reads.
        def get_text(page, option="text", **kwargs):
            if option == "text":
                calls.append(page.number)
            return real_get_text(page, option, **kwargs)

        with patch.object(pymupdf.Page, "get_text", get_text):
            (page,) = pdf_text.mupdf_pages(
                pdf, max_pages=10, max_chars=1 + header, tables=True
            )

        assert calls == []
        assert page == {"text": "", "tables": [[rows[0]]]}


@contextmanager
def loads_failing_from(first_bad_page):
    """Record each page PyMuPDF loads; loading ``first_bad_page`` or a later
    one fails, as a malformed page would."""
    real_load_page = pymupdf.Document.load_page
    loaded = []

    def load_page(doc, page_id):
        loaded.append(page_id)
        if page_id >= first_bad_page:
            raise RuntimeError(f"malformed page {page_id}")
        return real_load_page(doc, page_id)

    with patch.object(pymupdf.Document, "load_page", load_page):
        yield loaded


class TestReadMupdfPages:
    def test_returns_each_page_from_the_child(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])

        pages = pdf_text.read_mupdf_pages(pdf, 10, 10_000, timeout_secs=30.0)

        assert len(pages) == 2
        assert "Second page words" in pages[1]["text"]

    def test_unreadable_pdf_raises_value_error(self):
        with pytest.raises(ValueError):
            pdf_text.read_mupdf_pages(b"not a pdf", 10, 10_000, timeout_secs=30.0)

    def test_read_still_running_at_the_timeout_is_stopped(self):
        pdf = make_pdf_bytes(["Words"])
        children = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        # Far too short for the child even to import PyMuPDF.
        with patch.object(subprocess, "Popen", recording_popen):
            with pytest.raises(TimeoutError, match="did not finish within 0.01 seconds"):
                pdf_text.read_mupdf_pages(pdf, 10, 10_000, timeout_secs=0.01)

        assert len(children) == 1
        assert children[0].wait(timeout=5) == -signal.SIGKILL


class TestCountPdfPages:
    def test_counts_every_page(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        assert pdf_text.count_pdf_pages(pdf) == 3


@pytest.mark.asyncio
class TestAextractPdfPageTexts:
    async def test_returns_the_text_of_each_page(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])

        texts = await pdf_text.aextract_pdf_page_texts(
            pdf, max_pages=10, max_chars=10_000, timeout_secs=30.0
        )

        assert len(texts) == 2
        assert "First page words" in texts[0]
        assert "Second page words" in texts[1]

    async def test_parse_uses_almost_no_cpu_in_this_process(self):
        # Eight pages that share one long content stream: well over a second
        # of parsing, none of which should be spent in this process.
        pdf = make_shared_stream_pdf_bytes(page_count=8, text_operations=40_000)

        cpu_before = time.process_time()
        texts = await pdf_text.aextract_pdf_page_texts(
            pdf, max_pages=10, max_chars=1_000_000, timeout_secs=60.0
        )
        cpu_used = time.process_time() - cpu_before

        assert len(texts) == 8
        assert cpu_used < 0.5

    async def test_parse_still_running_at_the_timeout_is_stopped(self):
        # 200 pages that share one long content stream take about a minute to
        # read in full.
        pdf = make_shared_stream_pdf_bytes(page_count=200, text_operations=50_000)
        children = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with patch.object(subprocess, "Popen", recording_popen):
            with pytest.raises(TimeoutError, match="did not finish within 1 seconds"):
                await pdf_text.aextract_pdf_page_texts(
                    pdf, max_pages=500, max_chars=1_000_000, timeout_secs=1.0
                )
        assert time.monotonic() - started < 5

        # Nothing is left parsing in this process...
        cpu_before = time.process_time()
        await asyncio.sleep(1.0)
        assert time.process_time() - cpu_before < 0.5
        # ...and the child that was parsing has been killed.
        assert len(children) == 1
        assert children[0].wait(timeout=5) == -signal.SIGKILL

    async def test_pdf_that_needs_a_password_raises_value_error(self):
        pdf = make_pdf_bytes(["Locked words"], user_password="open-sesame")

        with pytest.raises(ValueError, match="cannot be decrypted"):
            await pdf_text.aextract_pdf_page_texts(
                pdf, max_pages=10, max_chars=10_000, timeout_secs=30.0
            )

    async def test_unreadable_pdf_raises_value_error(self):
        with pytest.raises(ValueError):
            await pdf_text.aextract_pdf_page_texts(
                b"%PDF-1.4 not really a pdf",
                max_pages=10,
                max_chars=10_000,
                timeout_secs=30.0,
            )

    async def test_long_shared_string_returns_at_most_max_chars_quickly(self):
        # 200 pages that share one string of 500,000 letters: 100 million
        # characters from a file of a few kilobytes, and close to a minute to
        # read in full.
        pdf = make_shared_long_string_pdf_bytes(page_count=200, string_length=500_000)

        started = time.monotonic()
        texts = await pdf_text.aextract_pdf_page_texts(
            pdf, max_pages=500, max_chars=10_000, timeout_secs=20.0
        )

        assert sum(len(text) for text in texts) <= 10_000
        assert time.monotonic() - started < 10


class TestRunChild:
    def test_child_that_writes_past_the_output_bound_is_stopped(self):
        # The page reads as 500,000 characters, far more than a pipe holds, so
        # the child is still writing when this process stops reading.
        pdf = make_shared_long_string_pdf_bytes(page_count=1, string_length=500_000)
        children = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(subprocess, "Popen", recording_popen):
            with pytest.raises(RuntimeError, match="wrote more than 1000 bytes"):
                pdf_text._run_child(
                    ["text", "10", "10000000"],
                    pdf,
                    deadline=time.monotonic() + 30.0,
                    max_output_bytes=1000,
                )

        assert len(children) == 1
        assert children[0].wait(timeout=5) == -signal.SIGKILL


class TestChildMain:
    """The child's entry point, run in this process.

    resource.setrlimit is always patched, so the test process keeps its own
    address space.
    """

    def _run(self, arguments, stdin, setrlimit_error=None):
        """Run the entry point on ``stdin``; return setrlimit, status and report."""
        stdout = io.StringIO()
        unlimited = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        with (
            patch.object(pdf_text.resource, "getrlimit", return_value=unlimited),
            patch.object(
                pdf_text.resource, "setrlimit", side_effect=setrlimit_error
            ) as setrlimit,
            patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin))),
            patch.object(sys, "stdout", stdout),
        ):
            status = pdf_text._child_main(arguments)
        return setrlimit, status, json.loads(stdout.getvalue())

    @linux_only
    def test_caps_its_address_space_at_the_child_limit(self):
        setrlimit, _, _ = self._run(["pages"], make_pdf_bytes(["Page one"]))

        setrlimit.assert_called_once_with(
            resource.RLIMIT_AS,
            (pdf_text._CHILD_ADDRESS_SPACE_BYTES, resource.RLIM_INFINITY),
        )

    @linux_only
    def test_reads_the_pdf_when_the_cap_cannot_be_set(self):
        pdf = make_pdf_bytes(["Page one", "Page two"])

        _, status, report = self._run(["pages"], pdf, setrlimit_error=ValueError)

        assert (status, report) == (0, {"result": 2})

    def test_memory_error_is_reported_as_an_error(self):
        with patch.object(pdf_text, "count_pdf_pages", side_effect=MemoryError):
            _, status, report = self._run(["pages"], make_pdf_bytes(["Page one"]))

        assert (status, report) == (1, {"error": "MemoryError"})


@pytest.mark.asyncio
class TestAcountPdfPages:
    async def test_counts_every_page(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        assert await pdf_text.acount_pdf_pages(pdf, timeout_secs=30.0) == 3


@pytest.mark.asyncio
class TestAmergePdfs:
    async def test_merges_inputs_in_order(self, tmp_path):
        first = tmp_path / "first.pdf"
        first.write_bytes(make_pdf_bytes(["First document"]))
        second = tmp_path / "second.pdf"
        second.write_bytes(make_pdf_bytes(["Second document", "Its appendix"]))
        target = str(tmp_path / "merged.pdf")

        await pdf_text.amerge_pdfs([str(first), str(second)], target, 30.0)

        reader = PdfReader(target)
        assert len(reader.pages) == 3
        assert "First document" in reader.pages[0].extract_text()
        assert "Its appendix" in reader.pages[2].extract_text()

    async def test_merge_that_outlasts_the_timeout_raises_timeout_error(self, tmp_path):
        source = tmp_path / "source.pdf"
        source.write_bytes(make_pdf_bytes(["Some page"]))

        with pytest.raises(TimeoutError, match="merge did not finish"):
            await pdf_text.amerge_pdfs(
                [str(source)], str(tmp_path / "merged.pdf"), timeout_secs=0.01
            )

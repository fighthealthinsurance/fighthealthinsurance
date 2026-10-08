"""Reading and merging fetched PDFs with pypdf, in a child process, under a timeout."""

import asyncio
import signal
import subprocess
import time
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from fighthealthinsurance import pdf_text
from tests.pdf_fixtures import make_pdf_bytes, make_shared_stream_pdf_bytes


class TestExtractPdfPageTexts:
    def test_reads_each_page_in_order(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])

        texts = pdf_text.extract_pdf_page_texts(pdf, max_pages=10)

        assert len(texts) == 2
        assert "First page words" in texts[0]
        assert "Second page words" in texts[1]

    def test_stops_reading_at_the_page_cap(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        texts = pdf_text.extract_pdf_page_texts(pdf, max_pages=2)

        assert len(texts) == 2
        assert not any("Page three" in text for text in texts)

    def test_opens_a_pdf_encrypted_with_an_empty_password(self):
        pdf = make_pdf_bytes(["Readable without a password"], user_password="")

        texts = pdf_text.extract_pdf_page_texts(pdf, max_pages=10)

        assert "Readable without a password" in texts[0]

    def test_pdf_that_needs_a_password_raises_value_error(self):
        pdf = make_pdf_bytes(["Locked words"], user_password="open-sesame")

        with pytest.raises(ValueError, match="cannot be decrypted"):
            pdf_text.extract_pdf_page_texts(pdf, max_pages=10)


class TestCountPdfPages:
    def test_counts_every_page(self):
        pdf = make_pdf_bytes(["Page one", "Page two", "Page three"])

        assert pdf_text.count_pdf_pages(pdf) == 3


@pytest.mark.asyncio
class TestAextractPdfPageTexts:
    async def test_returns_the_text_of_each_page(self):
        pdf = make_pdf_bytes(["First page words", "Second page words"])

        texts = await pdf_text.aextract_pdf_page_texts(
            pdf, max_pages=10, timeout_secs=30.0
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
            pdf, max_pages=10, timeout_secs=60.0
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
                    pdf, max_pages=500, timeout_secs=1.0
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
            await pdf_text.aextract_pdf_page_texts(pdf, max_pages=10, timeout_secs=30.0)

    async def test_unreadable_pdf_raises_value_error(self):
        with pytest.raises(ValueError):
            await pdf_text.aextract_pdf_page_texts(
                b"%PDF-1.4 not really a pdf", max_pages=10, timeout_secs=30.0
            )


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

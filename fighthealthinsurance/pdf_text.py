"""
Reading and merging fetched PDFs with pypdf.

pypdf is pure Python and CPU bound, and the time a read takes depends on what
the pages draw, not on the size of the file: many pages can draw one long
content stream, for example. So async callers reach it through
:func:`aextract_pdf_page_texts`, :func:`acount_pdf_pages` and
:func:`amerge_pdfs`, which run the work in a child Python process and stop
that process at a timeout, so the calling process never parses the PDF.
"""

import asyncio
import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Sequence, Union

from pypdf import PasswordType, PdfReader, PdfWriter

#: Threads that each wait on one child process at a time, so they also cap how
#: many children run at once. Text reads of fetched documents and the steps
#: that build an appeal PDF (page counts and merges) have a pool each, so slow
#: text reads never hold up an appeal. Both are separate from the default
#: executor, which every asyncio.to_thread call in the process shares.
_TEXT = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fhi-pdf-text")
_ASSEMBLY = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fhi-pdf-assembly")


def _open_pdf(source: Union[str, io.BytesIO]) -> PdfReader:
    """Open a PDF, decrypting it with the empty password when it is encrypted.

    The empty password is all most published PDFs need. One that needs a real
    password raises ValueError.
    """
    reader = PdfReader(source)

    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception as e:
            raise ValueError("PDF is encrypted and cannot be decrypted") from e
        if decrypted == PasswordType.NOT_DECRYPTED:
            raise ValueError("PDF is encrypted and cannot be decrypted")
    return reader


def extract_pdf_page_texts(content: bytes, max_pages: int) -> List[str]:
    """Return the text of each of the first ``max_pages`` pages, in order.

    Pages past ``max_pages`` are not read.
    """
    reader = _open_pdf(io.BytesIO(content))

    page_texts: List[str] = []
    for page_number, page in enumerate(reader.pages):
        if page_number >= max_pages:
            break
        page_texts.append(page.extract_text() or "")
    return page_texts


def count_pdf_pages(content: bytes) -> int:
    """Return how many pages the PDF has."""
    return len(_open_pdf(io.BytesIO(content)).pages)


def merge_pdfs(input_paths: Sequence[str], target: str) -> None:
    """Write the pages of each PDF in ``input_paths``, in order, to ``target``."""
    writer = PdfWriter()
    for path in input_paths:
        writer.append(_open_pdf(path))
    writer.write(target)
    writer.close()


def _run_child(arguments: Sequence[str], stdin: bytes, deadline: float) -> Any:
    """Run one operation in a child process and return its result.

    The child is killed if it is still running at ``deadline``, a
    time.monotonic() value. An error the child reports is raised here as
    ValueError.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(arguments, 0)
    # -P keeps this module's directory off the child's sys.path, so the child
    # imports the standard library and pypdf, never a sibling module.
    completed = subprocess.run(
        [sys.executable, "-P", __file__, *arguments],
        input=stdin,
        capture_output=True,
        timeout=remaining,
    )
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        raise RuntimeError(
            f"PDF worker exited with status {completed.returncode}"
        ) from None
    if "error" in report:
        raise ValueError(report["error"])
    return report["result"]


async def _arun_child(
    executor: ThreadPoolExecutor,
    arguments: Sequence[str],
    stdin: bytes,
    timeout_secs: float,
    description: str,
) -> Any:
    """Run :func:`_run_child` on ``executor`` without blocking the event loop.

    Raises TimeoutError when the operation takes longer than ``timeout_secs``,
    including any time spent waiting for a free thread. The child is killed at
    that same moment, so a timed-out operation does no further work.
    """
    deadline = time.monotonic() + timeout_secs
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, _run_child, arguments, stdin, deadline),
            timeout=timeout_secs,
        )
    except (asyncio.TimeoutError, subprocess.TimeoutExpired):
        raise TimeoutError(
            f"PDF {description} did not finish within {timeout_secs:g} seconds"
        ) from None


async def aextract_pdf_page_texts(
    content: bytes, max_pages: int, timeout_secs: float
) -> List[str]:
    """Run :func:`extract_pdf_page_texts` in a child process.

    Raises TimeoutError when it takes longer than ``timeout_secs``.
    """
    page_texts: List[str] = await _arun_child(
        _TEXT, ["text", str(max_pages)], content, timeout_secs, "text extraction"
    )
    return page_texts


async def acount_pdf_pages(content: bytes, timeout_secs: float) -> int:
    """Run :func:`count_pdf_pages` in a child process.

    Raises TimeoutError when it takes longer than ``timeout_secs``.
    """
    page_count: int = await _arun_child(
        _ASSEMBLY, ["pages"], content, timeout_secs, "page count"
    )
    return page_count


async def amerge_pdfs(
    input_paths: Sequence[str], target: str, timeout_secs: float
) -> None:
    """Run :func:`merge_pdfs` in a child process.

    Raises TimeoutError when it takes longer than ``timeout_secs``.
    """
    await _arun_child(
        _ASSEMBLY, ["merge", target, *input_paths], b"", timeout_secs, "merge"
    )


def _child_main(arguments: List[str]) -> int:
    """Run the operation named in ``arguments`` and print its result as JSON."""
    operation, rest = arguments[0], arguments[1:]
    result: Any = None
    try:
        if operation == "text":
            result = extract_pdf_page_texts(sys.stdin.buffer.read(), int(rest[0]))
        elif operation == "pages":
            result = count_pdf_pages(sys.stdin.buffer.read())
        elif operation == "merge":
            merge_pdfs(rest[1:], rest[0])
        else:
            raise ValueError(f"Unknown PDF operation {operation!r}")
    except Exception as e:
        json.dump({"error": str(e) or type(e).__name__}, sys.stdout)
        return 1
    json.dump({"result": result}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))

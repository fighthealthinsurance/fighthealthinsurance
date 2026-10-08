"""
Reading and merging fetched PDFs with pypdf.

pypdf is pure Python and CPU bound, and the time a read takes depends on what
the pages draw, not on the size of the file: many pages can draw one long
content stream, for example. So async callers reach it through
:func:`aextract_pdf_page_texts`, :func:`acount_pdf_pages` and
:func:`amerge_pdfs`, which run the work in a child Python process and stop
that process at a timeout, so the calling process never parses the PDF. On
Linux the child also caps its own address space, so a read that needs too much
memory fails with MemoryError instead.

The text a read returns is capped too: a few kilobytes of PDF can draw
millions of characters, so a text read stops once it has collected the
characters asked for, and the calling process reads no more of the child's
output than those characters take.
"""

import asyncio
import io
import json
import os
import resource
import selectors
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

#: The most bytes json.dump writes for one character of a string: an escaped
#: surrogate pair such as "\ud83d\ude00".
_JSON_BYTES_PER_CHAR = 12
#: Bytes json.dump writes around each page's text in a list: two quotes and
#: a ", " separator.
_JSON_BYTES_PER_PAGE = 4
#: Room for the rest of a child's report (the JSON around a result, or an
#: error message). It is the whole bound for a page count or a merge.
_REPORT_BYTES = 64 * 1024
#: The address space a child may map, on Linux. A few kilobytes of PDF can take
#: hundreds of megabytes to read and the pod sets no memory limit
#: (k8s/deploy.yaml), so a child stops with MemoryError here rather than
#: taking memory the web worker (about 0.9 GB) needs.
_CHILD_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024


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


def extract_pdf_page_texts(content: bytes, max_pages: int, max_chars: int) -> List[str]:
    """Return the text of each of the first ``max_pages`` pages, in order.

    The texts hold at most ``max_chars`` characters in all: reading stops once
    that many have been collected, and the last page read is cut to fit.
    Pages past ``max_pages`` are not read either.
    """
    reader = _open_pdf(io.BytesIO(content))

    page_texts: List[str] = []
    chars_left = max_chars
    for page_number, page in enumerate(reader.pages):
        if page_number >= max_pages or chars_left <= 0:
            break
        text = (page.extract_text() or "")[:chars_left]
        page_texts.append(text)
        chars_left -= len(text)
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


def _exchange(
    child: "subprocess.Popen[bytes]",
    stdin: bytes,
    deadline: float,
    max_output_bytes: int,
) -> bytes:
    """Write ``stdin`` to ``child`` and return what it writes to stdout.

    Raises subprocess.TimeoutExpired at ``deadline``, and RuntimeError as soon
    as the child has written more than ``max_output_bytes``. Either way
    nothing more is read.
    """
    assert child.stdin is not None and child.stdout is not None
    unsent = memoryview(stdin)
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        if unsent:
            os.set_blocking(child.stdin.fileno(), False)
            selector.register(child.stdin, selectors.EVENT_WRITE)
        else:
            child.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(child.args, 0)
            for key, _ in selector.select(remaining):
                if key.fileobj is child.stdin:
                    try:
                        unsent = unsent[os.write(key.fd, unsent) :]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        # The child stopped reading; its report says why.
                        unsent = unsent[:0]
                    if not unsent:
                        selector.unregister(child.stdin)
                        child.stdin.close()
                else:
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(child.stdout)
                    output += chunk
                    if len(output) > max_output_bytes:
                        raise RuntimeError(
                            f"PDF worker wrote more than {max_output_bytes} bytes"
                        )
    return bytes(output)


def _run_child(
    arguments: Sequence[str],
    stdin: bytes,
    deadline: float,
    max_output_bytes: int = _REPORT_BYTES,
) -> Any:
    """Run one operation in a child process and return its result.

    The child is killed if it is still running at ``deadline``, a
    time.monotonic() value, or as soon as it has written more than
    ``max_output_bytes`` to stdout, which raises RuntimeError. An error the
    child reports is raised here as ValueError.
    """
    if deadline - time.monotonic() <= 0:
        raise subprocess.TimeoutExpired(arguments, 0)
    # -P keeps this module's directory off the child's sys.path, so the child
    # imports the standard library and pypdf, never a sibling module. Its
    # stderr, where pypdf's warnings go, is discarded rather than collected.
    with subprocess.Popen(
        [sys.executable, "-P", __file__, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as child:
        try:
            output = _exchange(child, stdin, deadline, max_output_bytes)
            returncode = child.wait(timeout=max(deadline - time.monotonic(), 0))
        finally:
            # Does nothing when the child has already exited.
            child.kill()
    try:
        report = json.loads(output)
    except ValueError:
        raise RuntimeError(f"PDF worker exited with status {returncode}") from None
    if "error" in report:
        raise ValueError(report["error"])
    return report["result"]


async def _arun_child(
    executor: ThreadPoolExecutor,
    arguments: Sequence[str],
    stdin: bytes,
    timeout_secs: float,
    description: str,
    max_output_bytes: int = _REPORT_BYTES,
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
            loop.run_in_executor(
                executor, _run_child, arguments, stdin, deadline, max_output_bytes
            ),
            timeout=timeout_secs,
        )
    except (asyncio.TimeoutError, subprocess.TimeoutExpired):
        raise TimeoutError(
            f"PDF {description} did not finish within {timeout_secs:g} seconds"
        ) from None


async def aextract_pdf_page_texts(
    content: bytes, max_pages: int, max_chars: int, timeout_secs: float
) -> List[str]:
    """Run :func:`extract_pdf_page_texts` in a child process.

    Raises TimeoutError when it takes longer than ``timeout_secs``. This
    process reads no more of the child's output than ``max_pages`` pages
    holding ``max_chars`` characters can take, and raises RuntimeError if the
    child writes more.
    """
    max_output_bytes = (
        _JSON_BYTES_PER_CHAR * max_chars
        + _JSON_BYTES_PER_PAGE * max_pages
        + _REPORT_BYTES
    )
    page_texts: List[str] = await _arun_child(
        _TEXT,
        ["text", str(max_pages), str(max_chars)],
        content,
        timeout_secs,
        "text extraction",
        max_output_bytes,
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


def _limit_address_space() -> None:
    """Hold this process to ``_CHILD_ADDRESS_SPACE_BYTES`` of address space.

    Only Linux enforces RLIMIT_AS (macOS rejects it), so elsewhere this does
    nothing. A lower limit already in place is kept, and a limit that cannot
    be set is skipped rather than failing the operation.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if soft == resource.RLIM_INFINITY or soft > _CHILD_ADDRESS_SPACE_BYTES:
            resource.setrlimit(resource.RLIMIT_AS, (_CHILD_ADDRESS_SPACE_BYTES, hard))
    except (OSError, ValueError):
        pass


def _child_main(arguments: List[str]) -> int:
    """Run the operation named in ``arguments`` and print its result as JSON."""
    _limit_address_space()
    operation, rest = arguments[0], arguments[1:]
    result: Any = None
    try:
        if operation == "text":
            result = extract_pdf_page_texts(
                sys.stdin.buffer.read(), int(rest[0]), int(rest[1])
            )
        elif operation == "pages":
            result = count_pdf_pages(sys.stdin.buffer.read())
        elif operation == "merge":
            merge_pdfs(rest[1:], rest[0])
        else:
            raise ValueError(f"Unknown PDF operation {operation!r}")
    except Exception as e:
        error = str(e) or type(e).__name__
    else:
        json.dump({"result": result}, sys.stdout)
        return 0
    # Written once the except block has let go of the traceback, and with it
    # the failed operation's objects, so a MemoryError still has room to be
    # reported.
    json.dump({"error": error}, sys.stdout)
    return 1


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))

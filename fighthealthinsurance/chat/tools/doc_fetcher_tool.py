"""
Document fetcher tool handler for the chat interface.

Handles fetch_doc tool calls from the LLM to fetch and extract text
from documents at URLs shared by users (PDFs, DOCX, HTML, plain text).
"""

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any, Awaitable, Callable, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from loguru import logger

from fighthealthinsurance.chat.document_processor import process_uploaded_document
from fighthealthinsurance.extralink_fetcher import ExtraLinkFetcher

from .base_tool import BaseTool
from .patterns import FETCH_DOC_REGEX

# Maximum characters of extracted text to include in LLM context
MAX_CHAT_TEXT_LENGTH = 15_000

# Maximum number of fetch_doc calls allowed per chat session
MAX_FETCHES_PER_SESSION = 3


def _sanitize_url_for_display(url: str) -> str:
    """Strip sensitive URL components for safe display in status messages."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname is not None:
        display_host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{display_host}:{port}" if port is not None else display_host
    else:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


async def validate_url(url: str) -> None:
    """
    Validate a URL for safety (SSRF protection).

    Rejects non-HTTP(S) schemes, private/reserved IPs, and localhost.

    Note: There is a TOCTOU gap here — DNS is resolved for validation, but
    aiohttp resolves DNS again independently during the actual fetch. A DNS
    rebinding attack could bypass this check. For production hardening,
    consider using aiohttp with a custom TCPConnector that validates resolved
    IPs at connection time.

    Args:
        url: The URL to validate

    Raises:
        ValueError: If the URL is unsafe
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Only HTTP and HTTPS URLs are supported, got: {parsed.scheme}"
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    hostname_lower = hostname.lower()
    if hostname_lower in ("localhost", "127.0.0.1", "::1") or hostname_lower.endswith(
        ".local"
    ):
        raise ValueError(f"Cannot fetch from local addresses: {hostname}")

    # Run DNS resolution in a thread to avoid blocking the event loop
    loop = asyncio.get_running_loop()
    try:
        addr_infos = await loop.run_in_executor(
            None, socket.getaddrinfo, hostname, None
        )
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve hostname: {hostname}") from e

    for addr_info in addr_infos:
        ip_str = addr_info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not ip.is_global:
            raise ValueError(f"Cannot fetch from non-global IP address: {ip_str}")


class DocFetcherTool(BaseTool):
    """
    Tool handler for fetching and reading documents from URLs.

    When the LLM includes a fetch_doc call in its response, this tool:
    1. Extracts the URL from the JSON parameters
    2. Validates the URL for safety (SSRF protection)
    3. Fetches the document and extracts text
    4. Returns the text as context for the LLM to incorporate
    """

    pattern = FETCH_DOC_REGEX
    name = "Document Fetcher"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
        fetch_count: Optional[list[int]] = None,
        chat=None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback
        self.fetcher = ExtraLinkFetcher()
        self._fetch_count = fetch_count if fetch_count is not None else [0]
        self.chat = chat

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        previous_context_summary: str = "",
        history_for_llm: Any = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        json_str = match.group(1).strip()
        cleaned_response = self.clean_response(response_text, match)

        try:
            params = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in fetch_doc: {json_str}: {e}")
            return cleaned_response, context

        if not isinstance(params, dict):
            logger.warning(
                f"fetch_doc called with non-object JSON: {type(params).__name__}"
            )
            return cleaned_response, context

        url_value = params.get("url", "")
        url = url_value.strip() if isinstance(url_value, str) else ""
        if not url:
            logger.warning("fetch_doc called with empty URL")
            return cleaned_response, context

        # Rate limiting — increment before validation so DNS resolution
        # attempts also count toward the per-session limit
        if self._fetch_count[0] >= MAX_FETCHES_PER_SESSION:
            logger.warning(
                f"fetch_doc rate limit reached ({MAX_FETCHES_PER_SESSION} per session)"
            )
            await self.send_status_message(
                "Document fetch limit reached for this session."
            )
            return cleaned_response, context

        self._fetch_count[0] += 1

        try:
            await validate_url(url)
        except ValueError as e:
            logger.warning(f"URL validation failed for fetch_doc: {e}")
            await self.send_status_message(f"Cannot fetch document: {e}")
            return cleaned_response, context

        safe_url = _sanitize_url_for_display(url)
        await self.send_status_message(f"Fetching document from {safe_url}...")

        try:
            await self.send_status_message("Downloading and extracting content...")
            full_text, doc_type = await self.fetcher.fetch_and_extract_text(
                url,
                url_validator=validate_url,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch document from {safe_url}: {e}")
            await self.send_status_message(f"Failed to fetch document: {e}")
            return cleaned_response, context

        if not full_text or not full_text.strip():
            await self.send_status_message(
                "Could not extract any text from the document."
            )
            return cleaned_response, context

        await self.send_status_message(
            f"Extracted {len(full_text)} characters from {doc_type} document."
        )

        # Store full document for chunking, summarization, and back-searching
        if self.chat:
            try:
                await process_uploaded_document(
                    chat=self.chat,
                    document_name=safe_url,
                    full_text=full_text,
                )
                await self.send_status_message(
                    "Document stored for analysis and future reference."
                )
            except Exception as e:
                logger.warning(f"Failed to store fetched document: {e}")

        # Truncate for immediate LLM context only
        llm_text = full_text[:MAX_CHAT_TEXT_LENGTH]
        doc_context = (
            f"\n\nDocument fetched from {safe_url}:\n"
            f"{llm_text}\n\n"
            f"Please incorporate the relevant information from the document above.\n"
        )

        # Re-call LLM with document context if callback available
        if self.call_llm_callback and model_backends:
            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                doc_context,
                previous_context_summary,
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response += additional_response
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = context + additional_context
            elif additional_context:
                context = additional_context

        updated_context = context + doc_context if context else doc_context

        return cleaned_response, updated_context

"""
Document fetcher tool handler for the chat interface.

Handles fetch_doc tool calls from the LLM to fetch and extract text
from documents at URLs shared by users (PDFs, DOCX, HTML, plain text).
"""

import ipaddress
import json
import re
import socket
from typing import Any, Awaitable, Callable, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from loguru import logger

from fighthealthinsurance.extralink_fetcher import ExtraLinkFetcher

from .base_tool import BaseTool
from .patterns import FETCH_DOC_REGEX

# Maximum characters of extracted text to include in LLM context
MAX_CHAT_TEXT_LENGTH = 15_000

# Maximum number of fetch_doc calls allowed per chat session
MAX_FETCHES_PER_SESSION = 3


def _sanitize_url_for_display(url: str) -> str:
    """Strip query params and fragments from a URL for safe display in status messages."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def validate_url(url: str) -> None:
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

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")

    for addr_info in addr_infos:
        ip_str = addr_info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
                raise ValueError(
                    f"Cannot fetch from private/reserved IP address: {ip_str}"
                )
        except ValueError as e:
            if "Cannot fetch" in str(e):
                raise
            # If ip_address parsing fails, skip this entry
            continue


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
        call_llm_callback: Optional[Callable[..., Awaitable[Tuple[str, str]]]] = None,
        fetch_count: Optional[list] = None,
    ):
        """
        Initialize the document fetcher tool.

        Args:
            send_status_message: Async function to send status updates
            call_llm_callback: Callback to call LLM with additional context
            fetch_count: Mutable list used as a counter [current_count].
                Pass the same list across calls to enforce per-session rate limits.
        """
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback
        self.fetcher = ExtraLinkFetcher()
        self._fetch_count = fetch_count if fetch_count is not None else [0]

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

        url = params.get("url", "").strip()
        if not url:
            logger.warning("fetch_doc called with empty URL")
            return cleaned_response, context

        # Rate limiting
        if self._fetch_count[0] >= MAX_FETCHES_PER_SESSION:
            logger.warning(
                f"fetch_doc rate limit reached ({MAX_FETCHES_PER_SESSION} per session)"
            )
            await self.send_status_message(
                "Document fetch limit reached for this session."
            )
            return cleaned_response, context

        try:
            validate_url(url)
        except ValueError as e:
            logger.warning(f"URL validation failed for fetch_doc: {e}")
            await self.send_status_message(f"Cannot fetch document: {e}")
            return cleaned_response, context

        safe_url = _sanitize_url_for_display(url)
        await self.send_status_message(f"Fetching document from {safe_url}...")

        try:
            await self.send_status_message("Downloading and extracting content...")
            extracted_text, doc_type = await self.fetcher.fetch_and_extract_text(
                url, max_length=MAX_CHAT_TEXT_LENGTH
            )
        except Exception as e:
            logger.warning(f"Failed to fetch document from {safe_url}: {e}")
            await self.send_status_message(f"Failed to fetch document: {e}")
            return cleaned_response, context

        self._fetch_count[0] += 1

        if not extracted_text or not extracted_text.strip():
            await self.send_status_message(
                "Could not extract any text from the document."
            )
            return cleaned_response, context

        await self.send_status_message(
            f"Extracted {len(extracted_text)} characters from {doc_type} document."
        )

        doc_context = (
            f"\n\nDocument fetched from {safe_url}:\n"
            f"{extracted_text}\n\n"
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

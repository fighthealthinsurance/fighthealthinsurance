"""
Document fetcher tool handler for the chat interface.

Handles fetch_doc tool calls from the LLM to fetch and extract text
from documents at URLs shared by users (PDFs, DOCX, HTML, plain text).
"""

import ipaddress
import re
import socket
from typing import Any, Awaitable, Callable, Optional, Tuple
from urllib.parse import urlparse

from loguru import logger

from fighthealthinsurance.extralink_fetcher import ExtraLinkFetcher

from .base_tool import BaseTool
from .patterns import FETCH_DOC_REGEX

# Maximum characters of extracted text to include in LLM context
MAX_CHAT_TEXT_LENGTH = 15_000


def validate_url(url: str) -> None:
    """
    Validate a URL for safety (SSRF protection).

    Rejects non-HTTP(S) schemes, private/reserved IPs, and localhost.

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
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback
        self.fetcher = ExtraLinkFetcher()

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
        """
        Execute document fetch and incorporate results.

        Args:
            match: Regex match containing JSON with URL
            response_text: Current LLM response
            context: Current context string
            model_backends: LLM backends for follow-up calls
            previous_context_summary: Summary for context
            history_for_llm: Chat history
            depth: Current recursion depth
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional

        Returns:
            Tuple of (updated_response, updated_context)
        """
        import json

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

        try:
            validate_url(url)
        except ValueError as e:
            logger.warning(f"URL validation failed for fetch_doc: {e}")
            await self.send_status_message(f"Cannot fetch document: {e}")
            return cleaned_response, context

        await self.send_status_message(f"Fetching document from {url}...")

        try:
            content, content_type, file_size = await self.fetcher._fetch_url(url)
            doc_type = self.fetcher._detect_document_type(url, content_type)
            extracted_text = await self.fetcher._extract_text(content, doc_type)
        except Exception as e:
            logger.warning(f"Failed to fetch document from {url}: {e}")
            await self.send_status_message(f"Failed to fetch document: {e}")
            return cleaned_response, context

        if not extracted_text or not extracted_text.strip():
            await self.send_status_message(
                "Could not extract any text from the document."
            )
            return cleaned_response, context

        # Truncate to fit LLM context
        if len(extracted_text) > MAX_CHAT_TEXT_LENGTH:
            extracted_text = extracted_text[:MAX_CHAT_TEXT_LENGTH] + "..."

        await self.send_status_message(
            f"Extracted {len(extracted_text)} characters from {doc_type} document."
        )

        doc_context = (
            f"\n\nDocument fetched from {url}:\n"
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

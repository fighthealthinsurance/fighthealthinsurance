"""
ExtraLink document fetcher service.

This module provides functionality to fetch and extract text from external documents
linked in microsites. Supports PDF, DOCX, HTML, and plain text documents.
"""

import asyncio
import hashlib
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from django.db import models

import aiohttp
import PyPDF2
from asgiref.sync import sync_to_async
from bs4 import BeautifulSoup
from loguru import logger

from fighthealthinsurance.microsites import get_all_microsites
from fighthealthinsurance.models import (
    ExtraLinkDocument,
    ExtraLinkFetchLog,
    MicrositeExtraLink,
)


class ExtraLinkFetcher:
    """
    Service for fetching and extracting text from extralink documents.

    This class handles:
    - Fetching documents from URLs
    - Extracting text from various formats (PDF, DOCX, HTML, TXT)
    - Deduplicating based on URL hash
    - Storing documents and metadata in the database
    - Creating microsite-to-document associations
    """

    # Configuration limits
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
    FETCH_TIMEOUT = 60.0  # seconds
    MAX_TEXT_LENGTH = 1_000_000  # 1M characters

    async def prefetch_all_microsite_links(self) -> Dict[str, int]:
        """
        Pre-fetch all extralinks from all microsites.

        This is the main entry point for the deploy-time pre-fetch operation.
        It collects all unique URLs across all microsites and fetches them.

        Returns:
            Dict with counts of fetched/failed/skipped documents:
            {
                'fetched': int,
                'failed': int,
                'skipped': int,
                'total': int,
            }
        """
        microsites = await sync_to_async(get_all_microsites)()

        stats = {
            "fetched": 0,
            "failed": 0,
            "skipped": 0,
            "total": 0,
        }

        # Collect all unique URLs across all microsites
        url_to_microsites: Dict[str, List[Tuple]] = {}

        for slug, microsite in microsites.items():
            if not microsite.extralinks:
                continue

            for link_data in microsite.extralinks:
                url = link_data.get("url")
                if not url:
                    logger.warning(
                        f"Extralink in microsite '{slug}' missing URL: {link_data}"
                    )
                    continue

                if url not in url_to_microsites:
                    url_to_microsites[url] = []

                url_to_microsites[url].append((slug, link_data))

        stats["total"] = len(url_to_microsites)
        logger.info(f"Found {stats['total']} unique URLs across all microsites")

        # Fetch each unique URL
        for url, microsite_data in url_to_microsites.items():
            try:
                result = await self.fetch_and_store_document(url, microsite_data)

                if result == "fetched":
                    stats["fetched"] += 1
                elif result == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1

            except Exception as e:
                logger.opt(exception=True).warning(f"Error fetching {url}: {e}")
                stats["failed"] += 1

        logger.info(
            f"Pre-fetch complete: {stats['fetched']} fetched, "
            f"{stats['failed']} failed, {stats['skipped']} skipped"
        )

        return stats

    async def fetch_and_store_document(
        self,
        url: str,
        microsite_data: List[Tuple],
    ) -> str:
        """
        Fetch a single document and store it.

        Args:
            url: The URL to fetch
            microsite_data: List of (microsite_slug, link_data) tuples
                for all microsites that reference this URL

        Returns:
            'fetched', 'skipped', or 'failed'
        """
        url_hash = ExtraLinkDocument.get_url_hash(url)

        # Check if already successfully fetched
        existing = await ExtraLinkDocument.objects.filter(
            url_hash=url_hash,
            fetch_status="success",
        ).afirst()

        if existing:
            logger.debug(f"URL already fetched successfully: {url}")
            # Still ensure MicrositeExtraLink entries exist
            await self._ensure_microsite_links(existing, microsite_data)
            return "skipped"

        # Create or get document record
        doc, created = await ExtraLinkDocument.objects.aget_or_create(
            url_hash=url_hash,
            defaults={
                "url": url,
                "fetch_status": "pending",
            },
        )

        if not created and doc.fetch_status == "fetching":
            # Another process is fetching this
            logger.debug(f"URL is being fetched by another process: {url}")
            return "skipped"

        # Mark as fetching
        await ExtraLinkDocument.objects.filter(id=doc.id).aupdate(
            fetch_status="fetching",
            fetch_attempts=models.F("fetch_attempts") + 1,
        )

        # Log fetch start
        await ExtraLinkFetchLog.objects.acreate(
            document=doc,
            status="started",
            triggered_by="deploy",
        )

        start_time = time.time()

        try:
            # Fetch the document
            content, content_type, file_size = await self._fetch_url(url)

            # Determine document type
            doc_type = self._detect_document_type(url, content_type)

            # Extract text
            extracted_text = await self._extract_text(content, doc_type)

            # Truncate if needed
            if len(extracted_text) > self.MAX_TEXT_LENGTH:
                logger.warning(
                    f"Extracted text too long ({len(extracted_text)} chars), "
                    f"truncating to {self.MAX_TEXT_LENGTH}"
                )
                extracted_text = extracted_text[: self.MAX_TEXT_LENGTH]

            duration_ms = int((time.time() - start_time) * 1000)

            # Update document
            await ExtraLinkDocument.objects.filter(id=doc.id).aupdate(
                document_type=doc_type,
                content_type=content_type,
                file_size=file_size,
                raw_text=extracted_text,
                fetch_status="success",
                first_fetched_at=models.functions.Now(),
                last_fetched_at=models.functions.Now(),
            )

            # Log success
            await ExtraLinkFetchLog.objects.acreate(
                document=doc,
                status="success",
                triggered_by="deploy",
                fetch_duration_ms=duration_ms,
            )

            # Create MicrositeExtraLink entries
            await self._ensure_microsite_links(doc, microsite_data)

            logger.info(
                f"Successfully fetched {url} ({doc_type}, {len(extracted_text)} chars, "
                f"{duration_ms}ms)"
            )

            return "fetched"

        except Exception as e:
            error_msg = str(e)
            duration_ms = int((time.time() - start_time) * 1000)

            # Update document with failure
            await ExtraLinkDocument.objects.filter(id=doc.id).aupdate(
                fetch_status="failed",
                fetch_error=error_msg[:1000],
                last_fetched_at=models.functions.Now(),
            )

            # Log failure
            await ExtraLinkFetchLog.objects.acreate(
                document=doc,
                status="failed",
                error_message=error_msg[:1000],
                triggered_by="deploy",
                fetch_duration_ms=duration_ms,
            )

            logger.warning(f"Failed to fetch {url}: {error_msg}")

            return "failed"

    async def _fetch_url(self, url: str) -> Tuple[bytes, str, int]:
        """
        Fetch URL content via HTTP.

        Args:
            url: The URL to fetch

        Returns:
            Tuple of (content_bytes, content_type, file_size)

        Raises:
            Exception: If fetch fails or file is too large
        """
        timeout = aiohttp.ClientTimeout(total=self.FETCH_TIMEOUT)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "unknown")
                content = await response.read()

                if len(content) > self.MAX_FILE_SIZE:
                    raise ValueError(
                        f"File too large: {len(content)} bytes "
                        f"(max {self.MAX_FILE_SIZE})"
                    )

                return content, content_type, len(content)

    def _detect_document_type(self, url: str, content_type: str) -> str:
        """
        Detect document type from URL and content type.

        Args:
            url: The document URL
            content_type: HTTP Content-Type header

        Returns:
            Document type: 'pdf', 'docx', 'html', 'txt', or 'unknown'
        """
        url_lower = url.lower()
        ct_lower = content_type.lower()

        if "pdf" in ct_lower or url_lower.endswith(".pdf"):
            return "pdf"
        elif "word" in ct_lower or url_lower.endswith((".doc", ".docx")):
            return "docx"
        elif "html" in ct_lower or url_lower.endswith((".html", ".htm")):
            return "html"
        elif "text" in ct_lower or url_lower.endswith(".txt"):
            return "txt"
        else:
            return "unknown"

    async def _extract_text(self, content: bytes, doc_type: str) -> str:
        """
        Extract text from document content.

        Args:
            content: Raw document bytes
            doc_type: Detected document type

        Returns:
            Extracted text content

        Raises:
            Exception: If extraction fails
        """
        if doc_type == "pdf":
            return await self._extract_pdf_text(content)
        elif doc_type == "docx":
            return await self._extract_docx_text(content)
        elif doc_type == "html":
            return await self._extract_html_text(content)
        elif doc_type == "txt":
            return content.decode("utf-8", errors="ignore")
        else:
            # Try to decode as text
            try:
                return content.decode("utf-8", errors="ignore")
            except Exception:
                logger.warning(f"Could not decode content of type {doc_type}")
                return ""

    async def _extract_pdf_text(self, content: bytes) -> str:
        """
        Extract text from PDF using PyPDF2.

        Args:
            content: PDF file bytes

        Returns:
            Extracted text

        Raises:
            Exception: If PDF extraction fails
        """
        text_parts = []

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(content)
            tmp.flush()

            try:
                with open(tmp.name, "rb") as f:
                    reader = PyPDF2.PdfReader(f)

                    # Handle encrypted PDFs
                    if reader.is_encrypted:
                        try:
                            reader.decrypt("")
                        except Exception:
                            raise ValueError("PDF is encrypted and cannot be decrypted")

                    # Extract text from each page
                    for page in reader.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(page_text)

            except Exception as e:
                logger.warning(f"PDF extraction failed: {e}")
                raise

        return "\n\n".join(text_parts)

    async def _extract_docx_text(self, content: bytes) -> str:
        """
        Extract text from DOCX.

        Uses pandoc command-line tool if available. In the future,
        could use python-docx library for better support.

        Args:
            content: DOCX file bytes

        Returns:
            Extracted text

        Raises:
            Exception: If DOCX extraction fails
        """
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=True) as tmp_in:
            tmp_in.write(content)
            tmp_in.flush()

            with tempfile.NamedTemporaryFile(
                suffix=".txt", delete=True, mode="w"
            ) as tmp_out:
                tmp_out.close()

                try:
                    # Try using pandoc to convert to plain text
                    import subprocess

                    subprocess.run(
                        [
                            "pandoc",
                            tmp_in.name,
                            "-t",
                            "plain",
                            "-o",
                            tmp_out.name,
                        ],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )

                    with open(tmp_out.name, "r") as f:
                        text = f.read()

                    return text

                except (subprocess.CalledProcessError, FileNotFoundError) as e:
                    logger.debug(f"Pandoc extraction failed: {e}")
                    # Pandoc not available or failed
                    # In the future, could fall back to python-docx
                    return ""

    async def _extract_html_text(self, content: bytes) -> str:
        """
        Extract text from HTML using BeautifulSoup.

        Removes script tags, style tags, and navigation elements,
        then extracts the main text content.

        Args:
            content: HTML bytes

        Returns:
            Extracted text
        """
        try:
            html = content.decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            # Remove script and style tags
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            # Get text
            text = soup.get_text(separator="\n", strip=True)

            # Clean up whitespace
            lines = [line.strip() for line in text.splitlines()]
            text = "\n".join(line for line in lines if line)

            return text

        except Exception as e:
            logger.warning(f"HTML extraction failed: {e}")
            return ""

    async def _ensure_microsite_links(
        self,
        doc: ExtraLinkDocument,
        microsite_data: List[Tuple],
    ):
        """
        Ensure MicrositeExtraLink entries exist for all microsites using this doc.

        Args:
            doc: The ExtraLinkDocument instance
            microsite_data: List of (microsite_slug, link_data) tuples
        """
        for slug, link_data in microsite_data:
            priority_map = {
                "high": 0,
                "medium": 1,
                "low": 2,
                "0": 0,
                "1": 1,
                "2": 2,
                0: 0,
                1: 1,
                2: 2,
            }
            priority_raw = link_data.get("priority")
            priority = (
                priority_map.get(priority_raw, 1)
                if isinstance(priority_raw, str)
                else (priority_raw if priority_raw is not None else 1)
            )
            await MicrositeExtraLink.objects.aget_or_create(
                microsite_slug=slug,
                document=doc,
                defaults={
                    "title": link_data.get("title", ""),
                    "description": link_data.get("description", ""),
                    "category": link_data.get("category", ""),
                    "priority": priority,
                    "display_order": 0,  # Can be updated later if needed
                },
            )

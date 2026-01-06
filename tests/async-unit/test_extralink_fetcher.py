"""
Unit tests for ExtraLink fetcher functionality.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from fighthealthinsurance.extralink_fetcher import ExtraLinkFetcher
from fighthealthinsurance.models import ExtraLinkDocument, MicrositeExtraLink


@pytest.mark.asyncio
class TestExtraLinkFetcher:
    """Test the ExtraLinkFetcher service."""

    async def test_get_url_hash(self):
        """Test URL hash generation."""
        url = "https://example.com/test.pdf"
        hash1 = ExtraLinkDocument.get_url_hash(url)
        hash2 = ExtraLinkDocument.get_url_hash(url)

        # Same URL should produce same hash
        assert hash1 == hash2
        # Hash should be 64 characters (SHA256)
        assert len(hash1) == 64

        # Different URL should produce different hash
        hash3 = ExtraLinkDocument.get_url_hash("https://different.com/test.pdf")
        assert hash1 != hash3

    def test_detect_document_type(self):
        """Test document type detection."""
        fetcher = ExtraLinkFetcher()

        # Test PDF detection
        assert (
            fetcher._detect_document_type(
                "https://example.com/test.pdf", "application/pdf"
            )
            == "pdf"
        )
        assert (
            fetcher._detect_document_type("https://example.com/test.PDF", "unknown")
            == "pdf"
        )

        # Test DOCX detection
        assert (
            fetcher._detect_document_type("https://example.com/test.docx", "unknown")
            == "docx"
        )
        assert (
            fetcher._detect_document_type(
                "https://example.com/test.doc",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            == "docx"
        )

        # Test HTML detection
        assert (
            fetcher._detect_document_type(
                "https://example.com/page.html", "text/html"
            )
            == "html"
        )

        # Test TXT detection
        assert (
            fetcher._detect_document_type("https://example.com/file.txt", "text/plain")
            == "txt"
        )

        # Test unknown
        assert (
            fetcher._detect_document_type("https://example.com/file.xyz", "unknown")
            == "unknown"
        )

    @pytest.mark.asyncio
    async def test_extract_html_text(self):
        """Test HTML text extraction."""
        fetcher = ExtraLinkFetcher()

        html_content = b"""
        <html>
            <head><title>Test Page</title></head>
            <body>
                <nav>Navigation</nav>
                <h1>Main Heading</h1>
                <p>This is a paragraph.</p>
                <script>console.log('test');</script>
                <footer>Footer</footer>
            </body>
        </html>
        """

        text = await fetcher._extract_html_text(html_content)

        # Should include main content
        assert "Main Heading" in text
        assert "This is a paragraph." in text

        # Should exclude script, nav, footer
        assert "console.log" not in text
        assert "Navigation" not in text
        assert "Footer" not in text

    @pytest.mark.asyncio
    async def test_extract_text_dispatches_correctly(self):
        """Test that _extract_text dispatches to correct extractors."""
        fetcher = ExtraLinkFetcher()

        # Mock the individual extraction methods
        with patch.object(
            fetcher, "_extract_pdf_text", new_callable=AsyncMock
        ) as mock_pdf, patch.object(
            fetcher, "_extract_docx_text", new_callable=AsyncMock
        ) as mock_docx, patch.object(
            fetcher, "_extract_html_text", new_callable=AsyncMock
        ) as mock_html:

            mock_pdf.return_value = "PDF text"
            mock_docx.return_value = "DOCX text"
            mock_html.return_value = "HTML text"

            # Test PDF dispatch
            result = await fetcher._extract_text(b"content", "pdf")
            assert result == "PDF text"
            mock_pdf.assert_called_once()

            # Test DOCX dispatch
            result = await fetcher._extract_text(b"content", "docx")
            assert result == "DOCX text"
            mock_docx.assert_called_once()

            # Test HTML dispatch
            result = await fetcher._extract_text(b"content", "html")
            assert result == "HTML text"
            mock_html.assert_called_once()

            # Test TXT (direct decode)
            result = await fetcher._extract_text(b"plain text", "txt")
            assert result == "plain text"

    @pytest.mark.asyncio
    async def test_prefetch_all_microsite_links_empty(self):
        """Test pre-fetch with no microsites."""
        fetcher = ExtraLinkFetcher()

        with patch(
            "fighthealthinsurance.extralink_fetcher.get_all_microsites"
        ) as mock_get_microsites:
            mock_get_microsites.return_value = {}

            result = await fetcher.prefetch_all_microsite_links()

            assert result["total"] == 0
            assert result["fetched"] == 0
            assert result["failed"] == 0
            assert result["skipped"] == 0

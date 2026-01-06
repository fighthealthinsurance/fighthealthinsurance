"""
Integration tests for ExtraLink functionality.
"""

import pytest
from django.test import TestCase
from fighthealthinsurance.extralink_context_helper import ExtraLinkContextHelper
from fighthealthinsurance.models import ExtraLinkDocument, MicrositeExtraLink


class TestExtraLinkModels(TestCase):
    """Test ExtraLink database models."""

    def test_extralink_document_creation(self):
        """Test creating an ExtraLinkDocument."""
        url = "https://example.com/test.pdf"
        url_hash = ExtraLinkDocument.get_url_hash(url)

        doc = ExtraLinkDocument.objects.create(
            url=url,
            url_hash=url_hash,
            document_type="pdf",
            raw_text="Test content",
            fetch_status="success",
        )

        self.assertIsNotNone(doc.id)
        self.assertEqual(doc.url, url)
        self.assertEqual(doc.document_type, "pdf")
        self.assertEqual(doc.fetch_status, "success")

    def test_microsite_extralink_creation(self):
        """Test creating a MicrositeExtraLink."""
        url = "https://example.com/test.pdf"
        url_hash = ExtraLinkDocument.get_url_hash(url)

        doc = ExtraLinkDocument.objects.create(
            url=url,
            url_hash=url_hash,
            document_type="pdf",
            fetch_status="success",
        )

        link = MicrositeExtraLink.objects.create(
            microsite_slug="test-microsite",
            document=doc,
            title="Test Document",
            priority="high",
        )

        self.assertEqual(link.microsite_slug, "test-microsite")
        self.assertEqual(link.document, doc)
        self.assertEqual(link.priority, "high")

    def test_unique_constraint(self):
        """Test that microsite_slug + document must be unique."""
        url = "https://example.com/test.pdf"
        url_hash = ExtraLinkDocument.get_url_hash(url)

        doc = ExtraLinkDocument.objects.create(
            url=url,
            url_hash=url_hash,
            fetch_status="success",
        )

        # Create first link
        MicrositeExtraLink.objects.create(
            microsite_slug="test-microsite",
            document=doc,
        )

        # Attempting to create duplicate should fail
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            MicrositeExtraLink.objects.create(
                microsite_slug="test-microsite",
                document=doc,
            )


@pytest.mark.asyncio
class TestExtraLinkContextHelper(TestCase):
    """Test the ExtraLinkContextHelper service."""

    async def test_get_context_for_microsite_empty(self):
        """Test getting context when no extralinks exist."""
        context = await ExtraLinkContextHelper.get_context_for_microsite(
            "nonexistent-microsite"
        )
        self.assertEqual(context, "")

    async def test_get_context_for_microsite_with_documents(self):
        """Test getting context with extralink documents."""
        # Create test documents
        url1 = "https://example.com/doc1.pdf"
        doc1 = await ExtraLinkDocument.objects.acreate(
            url=url1,
            url_hash=ExtraLinkDocument.get_url_hash(url1),
            raw_text="This is document 1 content about medical guidelines.",
            fetch_status="success",
        )

        url2 = "https://example.com/doc2.pdf"
        doc2 = await ExtraLinkDocument.objects.acreate(
            url=url2,
            url_hash=ExtraLinkDocument.get_url_hash(url2),
            raw_text="This is document 2 content about treatment protocols.",
            fetch_status="success",
        )

        # Link to microsite
        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc1,
            title="Medical Guidelines",
            priority="high",
            display_order=1,
        )

        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc2,
            title="Treatment Protocols",
            priority="medium",
            display_order=2,
        )

        # Get context
        context = await ExtraLinkContextHelper.get_context_for_microsite(
            "test-microsite"
        )

        # Should include both documents
        self.assertIn("Medical Guidelines", context)
        self.assertIn("Treatment Protocols", context)
        self.assertIn(url1, context)
        self.assertIn(url2, context)
        self.assertIn("medical guidelines", context.lower())

    async def test_get_extralink_citations(self):
        """Test getting citations from extralinks."""
        # Create test document
        url = "https://example.com/guidelines.pdf"
        doc = await ExtraLinkDocument.objects.acreate(
            url=url,
            url_hash=ExtraLinkDocument.get_url_hash(url),
            fetch_status="success",
        )

        # Link to microsite
        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc,
            title="Clinical Guidelines",
            priority="high",
        )

        # Get citations
        citations = await ExtraLinkContextHelper.get_extralink_citations(
            "test-microsite"
        )

        self.assertEqual(len(citations), 1)
        self.assertIn("Clinical Guidelines", citations[0])
        self.assertIn(url, citations[0])

    async def test_priority_ordering(self):
        """Test that high priority documents come first."""
        # Create documents with different priorities
        url1 = "https://example.com/low.pdf"
        doc1 = await ExtraLinkDocument.objects.acreate(
            url=url1,
            url_hash=ExtraLinkDocument.get_url_hash(url1),
            raw_text="Low priority content",
            fetch_status="success",
        )

        url2 = "https://example.com/high.pdf"
        doc2 = await ExtraLinkDocument.objects.acreate(
            url=url2,
            url_hash=ExtraLinkDocument.get_url_hash(url2),
            raw_text="High priority content",
            fetch_status="success",
        )

        # Link with different priorities
        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc1,
            title="Low Priority",
            priority="low",
        )

        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc2,
            title="High Priority",
            priority="high",
        )

        # Get context
        context = await ExtraLinkContextHelper.get_context_for_microsite(
            "test-microsite"
        )

        # High priority should appear first
        high_pos = context.find("High Priority")
        low_pos = context.find("Low Priority")
        self.assertLess(high_pos, low_pos)

    async def test_has_extralinks(self):
        """Test checking if a microsite has extralinks."""
        # Should return False for non-existent microsite
        has_links = await ExtraLinkContextHelper.has_extralinks("nonexistent")
        self.assertFalse(has_links)

        # Create a document and link
        url = "https://example.com/test.pdf"
        doc = await ExtraLinkDocument.objects.acreate(
            url=url,
            url_hash=ExtraLinkDocument.get_url_hash(url),
            fetch_status="success",
        )

        await MicrositeExtraLink.objects.acreate(
            microsite_slug="has-links",
            document=doc,
        )

        # Should return True now
        has_links = await ExtraLinkContextHelper.has_extralinks("has-links")
        self.assertTrue(has_links)

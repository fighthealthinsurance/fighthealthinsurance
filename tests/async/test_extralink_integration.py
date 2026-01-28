"""
Integration tests for ExtraLink functionality.
"""

import pytest
from django.db import IntegrityError
from fighthealthinsurance.extralink_context_helper import ExtraLinkContextHelper
from fighthealthinsurance.models import ExtraLinkDocument, MicrositeExtraLink


@pytest.mark.django_db
class TestExtraLinkModels:
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

        assert doc.id is not None
        assert doc.url == url
        assert doc.document_type == "pdf"
        assert doc.fetch_status == "success"

    def test_microsite_extralink_creation(self):
        """Test creating a MicrositeExtraLink."""
        url = "https://example.com/test2.pdf"
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
            priority=0,
        )

        assert link.microsite_slug == "test-microsite"
        assert link.document == doc
        assert link.priority == 0

    def test_unique_constraint(self):
        """Test that microsite_slug + document must be unique."""
        url = "https://example.com/test3.pdf"
        url_hash = ExtraLinkDocument.get_url_hash(url)

        doc = ExtraLinkDocument.objects.create(
            url=url,
            url_hash=url_hash,
            fetch_status="success",
        )

        # Create first link
        MicrositeExtraLink.objects.create(
            microsite_slug="test-microsite-unique",
            document=doc,
        )

        # Attempting to create duplicate should fail
        with pytest.raises(IntegrityError):
            MicrositeExtraLink.objects.create(
                microsite_slug="test-microsite-unique",
                document=doc,
            )


@pytest.mark.django_db
class TestExtraLinkContextHelper:
    """Test the ExtraLinkContextHelper service."""

    @pytest.mark.asyncio
    @pytest.mark.django_db
    async def test_get_context_for_microsite_empty(self):
        """Test getting context when no extralinks exist."""
        context = await ExtraLinkContextHelper.get_context_for_microsite(
            "nonexistent-microsite"
        )
        assert context == ""

    @pytest.mark.asyncio
    @pytest.mark.django_db
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
            priority=0,
            display_order=1,
        )

        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc2,
            title="Treatment Protocols",
            priority=1,
            display_order=2,
        )

        # Get context
        context = await ExtraLinkContextHelper.get_context_for_microsite(
            "test-microsite"
        )

        # Should include both documents
        assert "Medical Guidelines" in context
        assert "Treatment Protocols" in context
        assert url1 in context
        assert url2 in context
        assert "medical guidelines" in context.lower()

    @pytest.mark.asyncio
    @pytest.mark.django_db
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
            priority=0,
        )

        # Get citations
        citations = await ExtraLinkContextHelper.get_extralink_citations(
            "test-microsite"
        )

        assert len(citations) == 1
        assert "Clinical Guidelines" in citations[0]
        assert url in citations[0]

    @pytest.mark.asyncio
    @pytest.mark.django_db
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
            priority=2,
        )

        await MicrositeExtraLink.objects.acreate(
            microsite_slug="test-microsite",
            document=doc2,
            title="High Priority",
            priority=0,
        )

        # Get context
        context = await ExtraLinkContextHelper.get_context_for_microsite(
            "test-microsite"
        )

        # High priority should appear first
        high_pos = context.find("High Priority")
        low_pos = context.find("Low Priority")
        assert high_pos < low_pos

    @pytest.mark.asyncio
    @pytest.mark.django_db
    async def test_has_extralinks(self):
        """Test checking if a microsite has extralinks."""
        # Should return False for non-existent microsite
        has_links = await ExtraLinkContextHelper.has_extralinks("nonexistent")
        assert has_links is False

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
        assert has_links is True

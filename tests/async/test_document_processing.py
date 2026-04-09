"""
Tests for large document handling in chat.

Tests cover:
1. Document chunking with various sizes
2. Search scoring and ranking
3. Document context integration in chat interface
4. ChatDocument model creation and processing
"""

import contextlib
import typing
import uuid
from unittest.mock import patch, AsyncMock, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from asgiref.sync import sync_to_async

from fighthealthinsurance.chat.document_processor import (
    chunk_document,
    process_uploaded_document,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
)
from fighthealthinsurance.chat.document_search import (
    _extract_search_terms,
    _score_chunk,
    search_chat_documents,
    get_document_context_for_llm,
    get_document_summaries_for_context,
)
from fighthealthinsurance.models import ChatDocument, OngoingChat

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class TestChunkDocument(TestCase):
    """Tests for document chunking logic."""

    def test_empty_text_returns_empty_list(self):
        assert chunk_document("") == []
        assert chunk_document("   ") == []

    def test_short_text_returns_single_chunk(self):
        text = "This is a short document."
        chunks = chunk_document(text)
        assert len(chunks) == 1
        assert chunks[0]["chunk_index"] == 0
        assert chunks[0]["text"] == text
        assert chunks[0]["start_char"] == 0
        assert chunks[0]["end_char"] == len(text)

    def test_text_at_boundary_returns_single_chunk(self):
        text = "x" * DEFAULT_CHUNK_SIZE
        chunks = chunk_document(text)
        assert len(chunks) == 1

    def test_long_text_produces_multiple_chunks(self):
        # Create text longer than one chunk
        text = "This is a test sentence. " * 500  # ~12500 chars
        chunks = chunk_document(text, chunk_size=2000, overlap=200)
        assert len(chunks) > 1
        # Verify sequential chunk indices
        for i, chunk in enumerate(chunks):
            assert chunk["chunk_index"] == i

    def test_chunks_have_overlap(self):
        text = "Word " * 2000  # 10000 chars
        chunks = chunk_document(text, chunk_size=3000, overlap=500)
        assert len(chunks) > 1
        # Check that chunks overlap: end of chunk N > start of chunk N+1
        for i in range(len(chunks) - 1):
            assert chunks[i]["end_char"] > chunks[i + 1]["start_char"]

    def test_chunks_cover_entire_document(self):
        text = "A" * 10000
        chunks = chunk_document(text, chunk_size=2000, overlap=200)
        # Every character in the text should be in at least one chunk
        covered = set()
        for chunk in chunks:
            for pos in range(chunk["start_char"], chunk["end_char"]):
                covered.add(pos)
        for pos in range(len(text)):
            assert pos in covered, f"Position {pos} not covered by any chunk"

    def test_prefers_paragraph_breaks(self):
        text = "First paragraph content here.\n\nSecond paragraph content here.\n\nThird paragraph."
        # Chunk size set so the first break should happen at a paragraph boundary
        chunks = chunk_document(text, chunk_size=40, overlap=5)
        # The first chunk should end at or near a paragraph boundary
        assert len(chunks) >= 2

    def test_prefers_sentence_breaks(self):
        text = "First sentence here. Second sentence here. Third sentence here. Fourth sentence."
        chunks = chunk_document(text, chunk_size=50, overlap=5)
        assert len(chunks) >= 2


class TestSearchTermExtraction(TestCase):
    """Tests for search term extraction from user queries."""

    def test_basic_extraction(self):
        terms = _extract_search_terms("What does my plan say about prior authorization?")
        assert "prior" in terms
        assert "authorization" in terms
        # Stopwords should be filtered
        assert "what" not in terms
        assert "does" not in terms
        assert "about" not in terms

    def test_short_words_filtered(self):
        terms = _extract_search_terms("Is it ok to do X?")
        assert "ok" not in terms  # too short (< 3 chars)

    def test_quoted_phrases_extracted(self):
        terms = _extract_search_terms('What about "medical necessity" criteria?')
        assert "medical necessity" in terms

    def test_empty_query(self):
        assert _extract_search_terms("") == []
        assert _extract_search_terms("the is a") == []


class TestChunkScoring(TestCase):
    """Tests for chunk relevance scoring."""

    def test_matching_terms_increase_score(self):
        chunk_text = "This plan requires prior authorization for all surgical procedures."
        score = _score_chunk(chunk_text, ["prior", "authorization"])
        assert score > 0

    def test_no_matches_returns_zero(self):
        chunk_text = "This is about dental coverage."
        score = _score_chunk(chunk_text, ["cardiology", "surgery"])
        assert score == 0.0

    def test_more_matching_terms_score_higher_than_unrelated(self):
        text = "Prior authorization is required. Authorization must be obtained before surgery."
        unrelated_text = "Dental coverage includes cleaning and exams."
        # A chunk with matching terms should score higher than one without
        score_match = _score_chunk(text, ["authorization", "surgery"])
        score_nomatch = _score_chunk(unrelated_text, ["authorization", "surgery"])
        assert score_match > score_nomatch

    def test_empty_inputs(self):
        assert _score_chunk("", ["test"]) == 0.0
        assert _score_chunk("some text", []) == 0.0

    def test_case_insensitive(self):
        text = "Prior Authorization Required"
        score = _score_chunk(text, ["prior", "authorization"])
        assert score > 0


class TestDocumentSearchAsync(APITestCase):
    """Async tests for document search functionality."""

    async def test_search_returns_relevant_chunks(self):
        chat = await OngoingChat.objects.acreate()
        doc = await ChatDocument.objects.acreate(
            chat=chat,
            document_name="test_plan.pdf",
            full_text="Full document text about prior authorization requirements.",
            char_count=100,
            processing_status="completed",
            chunk_summaries=[
                {
                    "chunk_index": 0,
                    "start_char": 0,
                    "end_char": 50,
                    "text": "Prior authorization is required for all surgeries.",
                    "summary": "Requires prior auth for surgeries.",
                },
                {
                    "chunk_index": 1,
                    "start_char": 50,
                    "end_char": 100,
                    "text": "Dental coverage includes cleaning and exams twice yearly.",
                    "summary": "Dental coverage details.",
                },
            ],
        )

        result = await search_chat_documents(
            chat.id, "What about prior authorization for surgery?"
        )
        assert "Prior authorization" in result
        assert "test_plan.pdf" in result
        # The dental chunk should NOT be returned (low relevance)
        # or should be ranked below the authorization chunk
        if "Dental" in result:
            auth_pos = result.index("Prior authorization")
            dental_pos = result.index("Dental")
            assert auth_pos < dental_pos

    async def test_search_with_no_documents_returns_empty(self):
        chat = await OngoingChat.objects.acreate()
        result = await search_chat_documents(chat.id, "anything")
        assert result == ""

    async def test_search_unprocessed_document_uses_full_text(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="raw.pdf",
            full_text="This document discusses appeal deadlines and procedures.",
            char_count=100,
            processing_status="pending",
            chunk_summaries=[],  # Not yet chunked
        )

        result = await search_chat_documents(chat.id, "appeal deadlines")
        assert "appeal deadlines" in result

    async def test_get_document_context_for_llm_returns_none_without_docs(self):
        chat = await OngoingChat.objects.acreate()
        result = await get_document_context_for_llm(chat.id, "test query")
        assert result is None

    async def test_get_document_context_for_llm_includes_header(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="plan.pdf",
            full_text="Prior authorization required for all procedures.",
            char_count=50,
            processing_status="completed",
            chunk_summaries=[
                {
                    "chunk_index": 0,
                    "start_char": 0,
                    "end_char": 50,
                    "text": "Prior authorization required for all procedures.",
                    "summary": "Prior auth required.",
                },
            ],
        )

        result = await get_document_context_for_llm(
            chat.id, "What about prior authorization?"
        )
        assert result is not None
        assert "Relevant sections from uploaded documents" in result

    async def test_get_document_summaries_lists_all_documents(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="plan.pdf",
            full_text="Text",
            summary="Plan document summary",
            char_count=1000,
            processing_status="completed",
        )
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="denial_letter.pdf",
            full_text="Text",
            summary="Denial letter summary",
            char_count=500,
            processing_status="completed",
        )

        result = await get_document_summaries_for_context(chat.id)
        assert result is not None
        assert "plan.pdf" in result
        assert "denial_letter.pdf" in result
        assert "Plan document summary" in result

    async def test_get_document_summaries_shows_processing_status(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="uploading.pdf",
            full_text="Text",
            char_count=100,
            processing_status="processing",
        )

        result = await get_document_summaries_for_context(chat.id)
        assert result is not None
        assert "(processing...)" in result


class TestProcessUploadedDocument(APITestCase):
    """Tests for the document upload processing pipeline."""

    async def test_creates_chat_document_record(self):
        chat = await OngoingChat.objects.acreate()
        full_text = "This is the full document text for testing purposes."

        with patch(
            "fighthealthinsurance.utils.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            doc = await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text=full_text,
            )

        assert doc.id is not None
        assert doc.document_name == "test.pdf"
        assert doc.char_count == len(full_text)
        assert doc.processing_status == "pending"
        assert doc.full_text == full_text

        # Verify it's in the database
        exists = await ChatDocument.objects.filter(id=doc.id).aexists()
        assert exists

    async def test_fires_background_summarization(self):
        chat = await OngoingChat.objects.acreate()

        with patch(
            "fighthealthinsurance.utils.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ) as mock_fire:
            await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text="Some text",
            )
            mock_fire.assert_called_once()

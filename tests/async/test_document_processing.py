"""
Tests for large document handling in chat.

Tests cover:
1. Document chunking with various sizes
2. Search scoring and ranking
3. Document context integration in chat interface
4. ChatDocument model creation and processing
"""

import asyncio
import typing
from unittest.mock import patch, AsyncMock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.document_processor import (
    DEFAULT_CHUNK_SIZE,
    DEFERRED_SUMMARY_WATCHDOG_SECONDS,
    STUCK_PROCESSING_RESCUE_SECONDS,
    _deferred_summarization_watchdog,
    chunk_document,
    process_uploaded_document,
    start_document_summarization,
)
from fighthealthinsurance.chat.document_search import (
    _extract_search_terms,
    _score_chunk,
    get_document_context_for_message,
)
from fighthealthinsurance.models import ChatDocument, OngoingChat

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def _fired_coroutine_names(mock_fire) -> list[str]:
    """Names of the coroutines handed to the mocked fire-and-forget helper."""
    return [call.args[0].__name__ for call in mock_fire.call_args_list]


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
        text = "This is a test sentence. " * 500
        chunks = chunk_document(text, chunk_size=2000, overlap=200)
        assert len(chunks) > 1
        for i, chunk in enumerate(chunks):
            assert chunk["chunk_index"] == i

    def test_chunks_have_overlap(self):
        text = "Word " * 2000
        chunks = chunk_document(text, chunk_size=3000, overlap=500)
        assert len(chunks) > 1
        for i in range(len(chunks) - 1):
            assert chunks[i]["end_char"] > chunks[i + 1]["start_char"]

    def test_chunks_cover_entire_document(self):
        text = "A" * 10000
        chunks = chunk_document(text, chunk_size=2000, overlap=200)
        covered = set()
        for chunk in chunks:
            for pos in range(chunk["start_char"], chunk["end_char"]):
                covered.add(pos)
        for pos in range(len(text)):
            assert pos in covered, f"Position {pos} not covered by any chunk"

    def test_prefers_paragraph_breaks(self):
        text = "First paragraph content here.\n\nSecond paragraph content here.\n\nThird paragraph."
        chunks = chunk_document(text, chunk_size=40, overlap=5)
        assert len(chunks) >= 2

    def test_prefers_sentence_breaks(self):
        text = "First sentence here. Second sentence here. Third sentence here. Fourth sentence."
        chunks = chunk_document(text, chunk_size=50, overlap=5)
        assert len(chunks) >= 2


class TestSearchTermExtraction(TestCase):
    """Tests for search term extraction from user queries."""

    def test_basic_extraction(self):
        terms = _extract_search_terms(
            "What does my plan say about prior authorization?"
        )
        assert "prior" in terms
        assert "authorization" in terms
        assert "what" not in terms
        assert "does" not in terms
        assert "about" not in terms

    def test_short_words_filtered(self):
        terms = _extract_search_terms("Is it ok to do X?")
        assert "ok" not in terms

    def test_quoted_phrases_extracted(self):
        terms = _extract_search_terms('What about "medical necessity" criteria?')
        assert "medical necessity" in terms

    def test_empty_query(self):
        assert _extract_search_terms("") == []
        assert _extract_search_terms("the is a") == []


class TestChunkScoring(TestCase):
    """Tests for chunk relevance scoring."""

    def test_matching_terms_increase_score(self):
        chunk_text = (
            "This plan requires prior authorization for all surgical procedures."
        )
        score = _score_chunk(chunk_text, ["prior", "authorization"])
        assert score > 0

    def test_no_matches_returns_zero(self):
        chunk_text = "This is about dental coverage."
        score = _score_chunk(chunk_text, ["cardiology", "surgery"])
        assert score == 0.0

    def test_more_matching_terms_score_higher_than_unrelated(self):
        text = "Prior authorization is required. Authorization must be obtained before surgery."
        unrelated_text = "Dental coverage includes cleaning and exams."
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


class TestDocumentContextAsync(APITestCase):
    """Async tests for document search and context retrieval."""

    async def test_returns_none_when_no_documents(self):
        chat = await OngoingChat.objects.acreate()
        result = await get_document_context_for_message(chat.id, "any query")
        assert result is None

    async def test_returns_relevant_chunks_for_matching_query(self):
        chat = await OngoingChat.objects.acreate()
        full_text = (
            "Prior authorization is required for all surgeries.\n"
            "Dental coverage includes cleaning and exams twice yearly."
        )
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="test_plan.pdf",
            full_text=full_text,
            char_count=len(full_text),
            processing_status=ChatDocument.Status.COMPLETED,
            chunk_summaries=[
                {
                    "chunk_index": 0,
                    "start_char": 0,
                    "end_char": 51,
                    "summary": "Requires prior auth for surgeries.",
                },
                {
                    "chunk_index": 1,
                    "start_char": 52,
                    "end_char": len(full_text),
                    "summary": "Dental coverage details.",
                },
            ],
        )

        result = await get_document_context_for_message(
            chat.id, "What about prior authorization for surgery?"
        )
        assert result is not None
        assert "Prior authorization" in result
        assert "test_plan.pdf" in result
        assert "Relevant sections" in result

    async def test_searches_unprocessed_document_full_text(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="raw.pdf",
            full_text="This document discusses appeal deadlines and procedures.",
            char_count=100,
            processing_status=ChatDocument.Status.PENDING,
            chunk_summaries=[],
        )

        result = await get_document_context_for_message(chat.id, "appeal deadlines")
        assert result is not None
        assert "appeal deadlines" in result

    async def test_lists_all_documents_in_summary_section(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="plan.pdf",
            full_text="Text",
            summary="Plan document summary",
            char_count=1000,
            processing_status=ChatDocument.Status.COMPLETED,
        )
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="denial_letter.pdf",
            full_text="Text",
            summary="Denial letter summary",
            char_count=500,
            processing_status=ChatDocument.Status.COMPLETED,
        )

        result = await get_document_context_for_message(chat.id, "hi")
        assert result is not None
        assert "Uploaded documents" in result
        assert "plan.pdf" in result
        assert "denial_letter.pdf" in result
        assert "Plan document summary" in result

    async def test_shows_processing_status_for_in_progress_docs(self):
        chat = await OngoingChat.objects.acreate()
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="uploading.pdf",
            full_text="Text",
            char_count=100,
            processing_status=ChatDocument.Status.PROCESSING,
        )

        result = await get_document_context_for_message(chat.id, "hi")
        assert result is not None
        assert "(processing)" in result


class TestProcessUploadedDocument(APITestCase):
    """Tests for the document upload processing pipeline."""

    async def test_creates_chat_document_record(self):
        chat = await OngoingChat.objects.acreate()
        full_text = "This is the full document text for testing purposes."

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
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
        # The immediate (non-deferred) path dispatches a worker, which claims
        # the row atomically -- so it leaves storage already PROCESSING.
        assert doc.processing_status == ChatDocument.Status.PROCESSING
        assert doc.full_text == full_text

        exists = await ChatDocument.objects.filter(id=doc.id).aexists()
        assert exists

    async def test_fires_background_summarization(self):
        chat = await OngoingChat.objects.acreate()

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ) as mock_fire:
            await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text="Some text",
            )
            mock_fire.assert_called_once()

    async def test_defer_summarization_arms_watchdog_but_no_worker(self):
        chat = await OngoingChat.objects.acreate()

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ) as mock_fire:
            doc = await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text="Some text",
                defer_summarization=True,
            )
            # Deferred: no summarization worker yet -- only the stranded-doc
            # watchdog is armed at storage time.
            assert _fired_coroutine_names(mock_fire) == [
                "_deferred_summarization_watchdog"
            ]
            assert doc.processing_status == ChatDocument.Status.PENDING

            # The caller kicks the worker off later; a PENDING doc fires.
            fired = await start_document_summarization(doc)
            assert fired
            assert _fired_coroutine_names(mock_fire) == [
                "_deferred_summarization_watchdog",
                "summarize_chunks",
            ]

            # A second kickoff loses the (already-spent) claim: no double fire.
            fired_again = await start_document_summarization(doc)
            assert not fired_again
            assert len(mock_fire.call_args_list) == 2

    async def test_start_summarization_claims_atomically_under_concurrency(self):
        chat = await OngoingChat.objects.acreate()

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ) as mock_fire:
            doc = await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text="Some text",
                defer_summarization=True,
            )
            # Two concurrent kickoffs (the turn's deferred start racing a
            # resubmission): the conditional-UPDATE claim lets exactly one
            # dispatch a worker.
            results = await asyncio.gather(
                start_document_summarization(doc),
                start_document_summarization(doc),
            )
            assert sorted(results) == [False, True]
            assert _fired_coroutine_names(mock_fire).count("summarize_chunks") == 1

    async def test_watchdog_rescues_stranded_pending_document(self):
        chat = await OngoingChat.objects.acreate()
        doc = await ChatDocument.objects.acreate(
            chat=chat,
            document_name="stranded.txt",
            full_text="Text stored by a turn that died before its kickoff.",
            char_count=51,
            processing_status=ChatDocument.Status.PENDING,
        )

        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(doc.id, None, delay=0.01)

        mock_summarize.assert_awaited_once_with(doc.id, denial_context=None)
        await doc.arefresh_from_db()
        assert doc.processing_status == ChatDocument.Status.PROCESSING

    async def test_watchdog_leaves_started_and_failed_documents_alone(self):
        chat = await OngoingChat.objects.acreate()
        # PROCESSING/COMPLETED were started normally; FAILED is deliberately
        # not retried by the watchdog (retries stay a resubmission decision).
        for status in (
            ChatDocument.Status.PROCESSING,
            ChatDocument.Status.COMPLETED,
            ChatDocument.Status.FAILED,
        ):
            doc = await ChatDocument.objects.acreate(
                chat=chat,
                document_name=f"{status}.txt",
                full_text=f"text in state {status}",
                char_count=10,
                processing_status=status,
            )
            with patch(
                "fighthealthinsurance.chat.document_processor.summarize_chunks",
                new_callable=AsyncMock,
            ) as mock_summarize:
                await _deferred_summarization_watchdog(doc.id, None, delay=0.01)
            mock_summarize.assert_not_awaited()
            await doc.arefresh_from_db()
            assert doc.processing_status == status

    async def test_watchdog_rescues_resubmitted_failed_document(self):
        # A FAILED doc the user resubmitted arms a watchdog claiming FAILED,
        # so the retry still happens even if the resubmitting turn dies.
        chat = await OngoingChat.objects.acreate()
        doc = await ChatDocument.objects.acreate(
            chat=chat,
            document_name="failed_retry.txt",
            full_text="content whose first analysis failed",
            char_count=35,
            processing_status=ChatDocument.Status.FAILED,
        )

        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(
                doc.id,
                None,
                delay=0.01,
                claim_statuses=[ChatDocument.Status.FAILED],
            )

        mock_summarize.assert_awaited_once_with(doc.id, denial_context=None)
        await doc.arefresh_from_db()
        assert doc.processing_status == ChatDocument.Status.PROCESSING

    async def test_deferred_resubmission_arms_watchdog_for_failed_status(self):
        chat = await OngoingChat.objects.acreate()
        full_text = "identical content resubmitted after a failed analysis"

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            first = await process_uploaded_document(
                chat=chat, document_name="a.txt", full_text=full_text
            )
            first.processing_status = ChatDocument.Status.FAILED
            await first.asave(update_fields=["processing_status"])

            with patch(
                "fighthealthinsurance.chat.document_processor._deferred_summarization_watchdog"
            ) as mock_watchdog:
                second = await process_uploaded_document(
                    chat=chat,
                    document_name="b.txt",
                    full_text=full_text,
                    defer_summarization=True,
                )

        assert second.id == first.id
        # The watchdog is armed to claim from the status observed at arming
        # time, so it can rescue this FAILED doc (not just PENDING ones).
        mock_watchdog.assert_called_once_with(
            first.id,
            None,
            DEFERRED_SUMMARY_WATCHDOG_SECONDS,
            claim_statuses=[ChatDocument.Status.FAILED],
        )

    async def test_resubmission_onto_processing_row_arms_stuck_rescue(self):
        # A worker can die without persisting a terminal status (pod restart,
        # OOM), leaving the row PROCESSING forever. Dedupe pins every future
        # resubmission to that row, so the resubmission must arm a rescue
        # watchdog that may claim PROCESSING after a generous delay.
        chat = await OngoingChat.objects.acreate()
        full_text = "content whose worker died mid-run without a terminal status"
        await ChatDocument.objects.acreate(
            chat=chat,
            document_name="stuck.txt",
            full_text=full_text,
            char_count=len(full_text),
            processing_status=ChatDocument.Status.PROCESSING,
        )

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            with patch(
                "fighthealthinsurance.chat.document_processor._deferred_summarization_watchdog"
            ) as mock_watchdog:
                doc = await process_uploaded_document(
                    chat=chat, document_name="retry.txt", full_text=full_text
                )

        mock_watchdog.assert_called_once_with(
            doc.id,
            None,
            STUCK_PROCESSING_RESCUE_SECONDS,
            claim_statuses=[ChatDocument.Status.PROCESSING],
        )

    async def test_fresh_document_does_not_arm_stuck_rescue(self):
        chat = await OngoingChat.objects.acreate()
        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            with patch(
                "fighthealthinsurance.chat.document_processor._deferred_summarization_watchdog"
            ) as mock_watchdog:
                await process_uploaded_document(
                    chat=chat, document_name="fresh.txt", full_text="brand new"
                )
        # The fresh doc dispatched its own worker; no rescue is needed.
        mock_watchdog.assert_not_called()

    async def test_watchdog_can_rescue_stuck_processing_when_told_to(self):
        chat = await OngoingChat.objects.acreate()
        doc = await ChatDocument.objects.acreate(
            chat=chat,
            document_name="stuck2.txt",
            full_text="text from a dead worker",
            char_count=23,
            processing_status=ChatDocument.Status.PROCESSING,
        )

        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(
                doc.id,
                None,
                delay=0.01,
                claim_statuses=[ChatDocument.Status.PROCESSING],
            )

        mock_summarize.assert_awaited_once_with(doc.id, denial_context=None)

    async def test_watchdog_handles_deleted_document(self):
        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(999999999, None, delay=0.01)
        mock_summarize.assert_not_awaited()

    async def test_start_summarization_skips_docs_already_processed(self):
        chat = await OngoingChat.objects.acreate()
        for status in (
            ChatDocument.Status.PROCESSING,
            ChatDocument.Status.COMPLETED,
        ):
            doc = await ChatDocument.objects.acreate(
                chat=chat,
                document_name=f"{status}.pdf",
                full_text="Text",
                char_count=4,
                processing_status=status,
            )
            with patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new_callable=AsyncMock,
            ) as mock_fire:
                fired = await start_document_summarization(doc)
                assert not fired
                mock_fire.assert_not_called()

    async def test_identical_resubmission_reuses_existing_document(self):
        chat = await OngoingChat.objects.acreate()
        full_text = "The same long pasted denial letter, resubmitted after a failure."

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            first = await process_uploaded_document(
                chat=chat,
                document_name="pasted_message_100.txt",
                full_text=full_text,
            )
            second = await process_uploaded_document(
                chat=chat,
                document_name="pasted_message_200.txt",
                full_text=full_text,
            )

        assert second.id == first.id
        # The original name wins so history references a real document.
        assert second.document_name == "pasted_message_100.txt"
        count = await ChatDocument.objects.filter(chat=chat).acount()
        assert count == 1

    async def test_different_content_is_not_deduped(self):
        chat = await OngoingChat.objects.acreate()

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            first = await process_uploaded_document(
                chat=chat,
                document_name="a.txt",
                full_text="first document text",
            )
            second = await process_uploaded_document(
                chat=chat,
                document_name="b.txt",
                full_text="second, different text",
            )

        assert second.id != first.id
        count = await ChatDocument.objects.filter(chat=chat).acount()
        assert count == 2

    async def test_same_content_in_a_different_chat_is_not_deduped(self):
        chat_a = await OngoingChat.objects.acreate()
        chat_b = await OngoingChat.objects.acreate()
        full_text = "shared text pasted into two unrelated chats"

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ):
            doc_a = await process_uploaded_document(
                chat=chat_a, document_name="a.txt", full_text=full_text
            )
            doc_b = await process_uploaded_document(
                chat=chat_b, document_name="b.txt", full_text=full_text
            )

        assert doc_a.id != doc_b.id

    async def test_resubmission_of_failed_document_refires_summarization(self):
        chat = await OngoingChat.objects.acreate()
        full_text = "content whose first summarization attempt failed"

        with patch(
            "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ) as mock_fire:
            first = await process_uploaded_document(
                chat=chat, document_name="a.txt", full_text=full_text
            )
            first.processing_status = ChatDocument.Status.FAILED
            await first.asave(update_fields=["processing_status"])

            second = await process_uploaded_document(
                chat=chat, document_name="b.txt", full_text=full_text
            )
            assert second.id == first.id
            # Once for the initial store, once for the FAILED retry.
            assert mock_fire.call_count == 2

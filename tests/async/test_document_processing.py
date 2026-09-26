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
from datetime import timedelta
from unittest.mock import patch, AsyncMock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.document_processor import (
    CHUNK_BATCH_SIZE,
    DEFAULT_CHUNK_SIZE,
    MAX_SUMMARIZATION_DEFERRAL_SECONDS,
    OVERALL_SUMMARY_TIMEOUT,
    SUMMARIZE_TIMEOUT,
    SUMMARY_MODEL_ATTEMPTS,
    WATCHDOG_GRACE_SECONDS,
    _abandonment_window_seconds,
    _deferred_summarization_watchdog,
    chunk_document,
    max_summarization_seconds,
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


async def _make_doc(
    chat,
    text: str = "Some stored text.",
    *,
    status: str = ChatDocument.Status.PENDING,
    age_seconds: float = 0,
) -> ChatDocument:
    """A stored ChatDocument whose char_count matches its text, optionally
    backdated (created_at is auto_now_add, so the age is set after insert)."""
    doc = await ChatDocument.objects.acreate(
        chat=chat,
        document_name="doc.txt",
        full_text=text,
        char_count=len(text),
        processing_status=status,
    )
    if age_seconds:
        doc.created_at = timezone.now() - timedelta(seconds=age_seconds)
        await ChatDocument.objects.filter(id=doc.id).aupdate(created_at=doc.created_at)
    return doc


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
    """Tests for the document upload processing pipeline.

    No test here may start real background summarization, which would reach
    ML backends: the fire-and-forget dispatcher is patched for every test (the
    coroutines it is handed are recorded, then closed unrun), and the watchdog
    tests call the watchdog coroutine directly with summarize_chunks patched.
    """

    def setUp(self):
        super().setUp()

        async def _record_and_discard(coro):
            coro.close()

        self.mock_fire = self.enterContext(
            patch(
                "fighthealthinsurance.chat.document_processor.fire_and_forget_in_new_threadpool",
                new=AsyncMock(side_effect=_record_and_discard),
            )
        )

    def fired(self) -> list[str]:
        """Names of the background coroutines dispatched so far."""
        return _fired_coroutine_names(self.mock_fire)

    # -- storage -----------------------------------------------------------

    async def test_creates_chat_document_record(self):
        chat = await OngoingChat.objects.acreate()
        full_text = "This is the full document text for testing purposes."

        doc = await process_uploaded_document(
            chat=chat, document_name="test.pdf", full_text=full_text
        )

        assert doc.id is not None
        assert doc.document_name == "test.pdf"
        assert doc.char_count == len(full_text)
        # The immediate (non-deferred) path dispatches a worker, which claims
        # the row atomically -- so it leaves storage already PROCESSING.
        assert doc.processing_status == ChatDocument.Status.PROCESSING
        assert doc.full_text == full_text
        assert await ChatDocument.objects.filter(id=doc.id).aexists()

    async def test_fires_background_summarization(self):
        chat = await OngoingChat.objects.acreate()

        await process_uploaded_document(
            chat=chat, document_name="test.pdf", full_text="Some text"
        )

        assert self.fired() == ["summarize_chunks"]

    async def test_storage_completes_when_caller_is_cancelled(self):
        # A disconnect can cancel the turn while the INSERT is in flight. The
        # storage protocol is shielded, so the row still gets written AND its
        # watchdog armed -- never a committed row with nothing to analyze it.
        chat = await OngoingChat.objects.acreate()
        real_acreate = ChatDocument.objects.acreate
        create_started = asyncio.Event()
        finish_create = asyncio.Event()

        async def slow_acreate(**kwargs):
            create_started.set()
            await finish_create.wait()
            return await real_acreate(**kwargs)

        with patch.object(ChatDocument.objects, "acreate", side_effect=slow_acreate):
            caller = asyncio.ensure_future(
                process_uploaded_document(
                    chat=chat,
                    document_name="a.txt",
                    full_text="text stored while the client disconnects",
                    defer_summarization_for=240,
                )
            )
            await create_started.wait()
            caller.cancel()
            finish_create.set()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            for _ in range(200):
                if self.mock_fire.called:
                    break
                await asyncio.sleep(0.01)

        assert await ChatDocument.objects.filter(chat=chat).aexists()
        assert self.fired() == ["_deferred_summarization_watchdog"]

    # -- deferred kickoff --------------------------------------------------

    async def test_deferred_storage_arms_watchdog_but_dispatches_no_worker(self):
        chat = await OngoingChat.objects.acreate()

        doc = await process_uploaded_document(
            chat=chat,
            document_name="test.pdf",
            full_text="Some text",
            defer_summarization_for=240,
        )

        assert self.fired() == ["_deferred_summarization_watchdog"]
        assert doc.processing_status == ChatDocument.Status.PENDING

    async def test_deferred_kickoff_dispatches_a_worker_exactly_once(self):
        chat = await OngoingChat.objects.acreate()
        doc = await process_uploaded_document(
            chat=chat,
            document_name="test.pdf",
            full_text="Some text",
            defer_summarization_for=240,
        )

        first = await start_document_summarization(doc)
        second = await start_document_summarization(doc)

        assert (first, second) == (True, False)
        assert self.fired().count("summarize_chunks") == 1

    async def test_watchdog_fires_grace_seconds_after_the_callers_deadline(self):
        chat = await OngoingChat.objects.acreate()
        with patch(
            "fighthealthinsurance.chat.document_processor._deferred_summarization_watchdog"
        ) as mock_watchdog:
            doc = await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text="Some text",
                defer_summarization_for=240,
            )

        mock_watchdog.assert_called_once_with(
            doc.id,
            None,
            240 + WATCHDOG_GRACE_SECONDS,
            claim_statuses=[ChatDocument.Status.PENDING],
        )

    async def test_deferral_is_clamped_to_the_maximum(self):
        chat = await OngoingChat.objects.acreate()
        with patch(
            "fighthealthinsurance.chat.document_processor._deferred_summarization_watchdog"
        ) as mock_watchdog:
            await process_uploaded_document(
                chat=chat,
                document_name="test.pdf",
                full_text="Some text",
                defer_summarization_for=MAX_SUMMARIZATION_DEFERRAL_SECONDS * 10,
            )

        delay = mock_watchdog.call_args.args[2]
        assert delay == MAX_SUMMARIZATION_DEFERRAL_SECONDS + WATCHDOG_GRACE_SECONDS

    async def test_deferred_resubmission_arms_watchdog_for_failed_status(self):
        chat = await OngoingChat.objects.acreate()
        text = "identical content resubmitted after a failed analysis"
        first = await _make_doc(chat, text, status=ChatDocument.Status.FAILED)

        with patch(
            "fighthealthinsurance.chat.document_processor._deferred_summarization_watchdog"
        ) as mock_watchdog:
            second = await process_uploaded_document(
                chat=chat,
                document_name="b.txt",
                full_text=text,
                defer_summarization_for=240,
            )

        assert second.id == first.id
        # The watchdog claims from the status observed at arming time, so it
        # can rescue this FAILED doc (not just PENDING ones).
        mock_watchdog.assert_called_once_with(
            first.id,
            None,
            240 + WATCHDOG_GRACE_SECONDS,
            claim_statuses=[ChatDocument.Status.FAILED],
        )

    # -- atomic claim ------------------------------------------------------

    async def test_start_summarization_claims_atomically_under_concurrency(self):
        # The turn's deferred kickoff racing a resubmission: the
        # conditional-UPDATE claim lets exactly one dispatch a worker.
        chat = await OngoingChat.objects.acreate()
        doc = await _make_doc(chat)

        results = await asyncio.gather(
            start_document_summarization(doc),
            start_document_summarization(doc),
        )

        assert sorted(results) == [False, True]
        assert self.fired().count("summarize_chunks") == 1

    async def test_start_summarization_skips_docs_already_processed(self):
        chat = await OngoingChat.objects.acreate()
        for status in (
            ChatDocument.Status.PROCESSING,
            ChatDocument.Status.COMPLETED,
        ):
            doc = await _make_doc(chat, f"text in state {status}", status=status)

            assert not await start_document_summarization(doc)
        assert self.fired() == []

    # -- watchdog ----------------------------------------------------------

    async def test_watchdog_rescues_stranded_pending_document(self):
        chat = await OngoingChat.objects.acreate()
        doc = await _make_doc(chat, "Text stored by a turn that died early.")

        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(doc.id, None, delay=0.01)

        mock_summarize.assert_awaited_once_with(doc.id, denial_context=None)
        await doc.arefresh_from_db()
        assert doc.processing_status == ChatDocument.Status.PROCESSING

    async def test_watchdog_leaves_started_and_failed_documents_alone(self):
        # PROCESSING/COMPLETED were started normally; FAILED is deliberately
        # not retried by a PENDING watchdog (retries stay a resubmission
        # decision).
        chat = await OngoingChat.objects.acreate()
        for status in (
            ChatDocument.Status.PROCESSING,
            ChatDocument.Status.COMPLETED,
            ChatDocument.Status.FAILED,
        ):
            doc = await _make_doc(chat, f"text in state {status}", status=status)
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
        doc = await _make_doc(
            chat,
            "content whose first analysis failed",
            status=ChatDocument.Status.FAILED,
        )

        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(
                doc.id, None, delay=0.01, claim_statuses=[ChatDocument.Status.FAILED]
            )

        mock_summarize.assert_awaited_once_with(doc.id, denial_context=None)

    async def test_watchdog_handles_deleted_document(self):
        with patch(
            "fighthealthinsurance.chat.document_processor.summarize_chunks",
            new_callable=AsyncMock,
        ) as mock_summarize:
            await _deferred_summarization_watchdog(999999999, None, delay=0.01)

        mock_summarize.assert_not_awaited()

    # -- deduplication -----------------------------------------------------

    async def test_identical_resubmission_reuses_existing_document(self):
        chat = await OngoingChat.objects.acreate()
        text = "The same long pasted denial letter, resubmitted after a failure."
        first = await process_uploaded_document(
            chat=chat, document_name="pasted_message_100.txt", full_text=text
        )

        second = await process_uploaded_document(
            chat=chat, document_name="pasted_message_200.txt", full_text=text
        )

        assert second.id == first.id
        # The original name wins so history references a real document.
        assert second.document_name == "pasted_message_100.txt"
        assert await ChatDocument.objects.filter(chat=chat).acount() == 1

    async def test_different_content_is_not_deduped(self):
        chat = await OngoingChat.objects.acreate()
        first = await process_uploaded_document(
            chat=chat, document_name="a.txt", full_text="first document text"
        )

        second = await process_uploaded_document(
            chat=chat, document_name="b.txt", full_text="second, different text"
        )

        assert second.id != first.id

    async def test_same_content_in_a_different_chat_is_not_deduped(self):
        text = "shared text pasted into two unrelated chats"
        doc_a = await process_uploaded_document(
            chat=await OngoingChat.objects.acreate(),
            document_name="a.txt",
            full_text=text,
        )

        doc_b = await process_uploaded_document(
            chat=await OngoingChat.objects.acreate(),
            document_name="b.txt",
            full_text=text,
        )

        assert doc_a.id != doc_b.id

    async def test_resubmission_of_failed_document_refires_summarization(self):
        chat = await OngoingChat.objects.acreate()
        text = "content whose first summarization attempt failed"
        first = await _make_doc(chat, text, status=ChatDocument.Status.FAILED)

        second = await process_uploaded_document(
            chat=chat, document_name="b.txt", full_text=text
        )

        assert second.id == first.id
        assert self.fired() == ["summarize_chunks"]

    async def test_resubmission_onto_live_processing_document_starts_nothing(self):
        # A worker is still (legitimately) running: reuse the row and neither
        # dispatch a second worker nor arm anything that could.
        chat = await OngoingChat.objects.acreate()
        text = "content whose analysis is still in flight"
        live = await _make_doc(
            chat, text, status=ChatDocument.Status.PROCESSING, age_seconds=60
        )

        doc = await process_uploaded_document(
            chat=chat, document_name="b.txt", full_text=text
        )

        assert doc.id == live.id
        assert self.fired() == []

    async def test_abandoned_processing_document_gets_a_fresh_copy(self):
        # Still PROCESSING past its abandonment window: its worker is gone
        # (e.g. killed by a pod restart), so reusing it would pin every
        # re-paste to a document nothing will ever analyze.
        chat = await OngoingChat.objects.acreate()
        text = "content whose worker died with the pod"
        dead = await _make_doc(
            chat,
            text,
            status=ChatDocument.Status.PROCESSING,
            age_seconds=_abandonment_window_seconds(text) + 60,
        )

        doc = await process_uploaded_document(
            chat=chat, document_name="b.txt", full_text=text
        )

        assert doc.id != dead.id
        assert self.fired() == ["summarize_chunks"]

    async def test_abandoned_failed_document_gets_a_fresh_copy(self):
        chat = await OngoingChat.objects.acreate()
        text = "content that failed long ago"
        old = await _make_doc(
            chat,
            text,
            status=ChatDocument.Status.FAILED,
            age_seconds=_abandonment_window_seconds(text) + 60,
        )

        doc = await process_uploaded_document(
            chat=chat, document_name="b.txt", full_text=text
        )

        assert doc.id != old.id

    async def test_completed_document_is_reused_however_old(self):
        chat = await OngoingChat.objects.acreate()
        text = "content analyzed long ago"
        done = await _make_doc(
            chat,
            text,
            status=ChatDocument.Status.COMPLETED,
            age_seconds=_abandonment_window_seconds(text) * 10,
        )

        doc = await process_uploaded_document(
            chat=chat, document_name="b.txt", full_text=text
        )

        assert doc.id == done.id
        assert self.fired() == []


class TestSummarizationTimeBounds(TestCase):
    """The abandonment window is only sound if max_summarization_seconds
    really bounds a live worker."""

    def test_bound_counts_every_batch_of_chunks(self):
        text = "Coverage was denied for lack of medical necessity. " * 2000
        batches = -(-len(chunk_document(text)) // CHUNK_BATCH_SIZE)
        assert max_summarization_seconds(text) == SUMMARY_MODEL_ATTEMPTS * (
            batches * SUMMARIZE_TIMEOUT + OVERALL_SUMMARY_TIMEOUT
        )

    def test_bound_grows_with_document_size(self):
        small = "A short denial letter. " * 100
        large = small * 50
        assert max_summarization_seconds(large) > max_summarization_seconds(small)

    def test_abandonment_window_exceeds_the_bound(self):
        text = "Coverage was denied for lack of medical necessity. " * 2000
        assert _abandonment_window_seconds(text) > max_summarization_seconds(text)

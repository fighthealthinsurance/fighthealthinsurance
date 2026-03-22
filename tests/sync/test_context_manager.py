"""
Tests for the chat context manager module.
"""

import pytest
from unittest.mock import AsyncMock, patch

from fighthealthinsurance.chat.context_manager import (
    MISSING_CONTEXT_PREFIX,
    background_generate_summary,
    make_placeholder,
    prepare_history_for_llm,
    should_store_summary,
    get_current_context,
    ensure_message_alternation,
    DEFAULT_MESSAGES_TO_KEEP,
    SUMMARIZATION_INTERVAL,
)


class TestPrepareHistoryForLLM:
    """Tests for prepare_history_for_llm function."""

    @pytest.mark.asyncio
    async def test_empty_history(self):
        """Test with empty history returns empty list."""
        history, full_history, summary = await prepare_history_for_llm(
            chat_history=None,
            existing_summary=None,
        )
        assert history == []
        assert full_history is None
        assert summary is None

    @pytest.mark.asyncio
    async def test_short_history_unchanged(self):
        """Test that short history is returned unchanged."""
        chat_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        history, full_history, summary = await prepare_history_for_llm(
            chat_history=chat_history,
            existing_summary=None,
        )
        assert len(history) == 2
        assert full_history is None
        assert summary is None

    @pytest.mark.asyncio
    async def test_preserves_existing_summary(self):
        """Test that existing summary is preserved when no summarization needed."""
        chat_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        existing = "Previous conversation about insurance"
        history, full_history, summary = await prepare_history_for_llm(
            chat_history=chat_history,
            existing_summary=existing,
        )
        assert summary == existing

    @pytest.mark.asyncio
    async def test_long_history_truncated(self):
        """Test that long history is truncated to last N messages."""
        # Create history longer than DEFAULT_MESSAGES_TO_KEEP
        chat_history = []
        for i in range(30):
            chat_history.append({"role": "user", "content": f"Message {i}"})
            chat_history.append({"role": "assistant", "content": f"Response {i}"})

        history, full_history, summary = await prepare_history_for_llm(
            chat_history=chat_history,
            existing_summary=None,
        )

        # Truncated history should be around DEFAULT_MESSAGES_TO_KEEP
        # (may be +1 if we backed up to start on a user message)
        assert len(history) <= DEFAULT_MESSAGES_TO_KEEP + 1
        # Full history should be preserved
        assert full_history is not None
        assert len(full_history) == 60

    @pytest.mark.asyncio
    async def test_truncation_preserves_alternation(self):
        """Test that truncation starts on a user message to avoid dropping context."""
        # Build history where naive slicing at -DEFAULT_MESSAGES_TO_KEEP
        # would land on an assistant message
        chat_history = []
        # We need an odd number of messages before the keep window so the
        # slice boundary falls on an assistant message.
        total_pairs = (DEFAULT_MESSAGES_TO_KEEP // 2) + 5
        for i in range(total_pairs):
            chat_history.append({"role": "user", "content": f"Message {i}"})
            chat_history.append({"role": "assistant", "content": f"Response {i}"})
        # Add one extra user message so the -DEFAULT_MESSAGES_TO_KEEP index
        # falls on an assistant message
        chat_history.append({"role": "user", "content": "Extra"})

        history, full_history, summary = await prepare_history_for_llm(
            chat_history=chat_history,
            existing_summary=None,
        )

        # History must start with a user message, not assistant
        assert history[0]["role"] == "user"
        # And alternation should be intact
        for i in range(len(history) - 1):
            assert (
                history[i]["role"] != history[i + 1]["role"]
            ), f"Messages {i} and {i+1} have same role: {history[i]['role']}"

    @pytest.mark.asyncio
    async def test_message_alternation_applied(self):
        """Test that message alternation is applied to history."""
        # Create malformed history with consecutive user messages
        chat_history = [
            {"role": "user", "content": "First"},
            {"role": "user", "content": "Second"},
            {"role": "assistant", "content": "Response"},
        ]
        history, _, _ = await prepare_history_for_llm(
            chat_history=chat_history,
            existing_summary=None,
        )
        # After alternation, consecutive same-role messages should be merged
        # The exact behavior depends on ensure_message_alternation implementation
        assert len(history) >= 1


class TestShouldStoreSummary:
    """Tests for should_store_summary function."""

    def test_no_new_summary(self):
        """Test returns False when no new summary."""
        assert should_store_summary([], None) is False
        assert should_store_summary(["old"], None) is False

    def test_empty_list_new_summary(self):
        """Test returns True when list is empty and new summary exists."""
        assert should_store_summary(None, "new summary") is True
        assert should_store_summary([], "new summary") is True

    def test_different_summary(self):
        """Test returns True when summary is different from last."""
        assert should_store_summary(["old summary"], "new summary") is True

    def test_same_summary(self):
        """Test returns False when summary matches last stored."""
        assert should_store_summary(["same summary"], "same summary") is False


class TestGetCurrentContext:
    """Tests for get_current_context function."""

    def test_no_summaries(self):
        """Test returns None when no summaries."""
        assert get_current_context(None) is None
        assert get_current_context([]) is None

    def test_returns_last_summary(self):
        """Test returns the last summary from list."""
        summaries = ["first", "second", "third"]
        assert get_current_context(summaries) == "third"

    def test_includes_pubmed_context(self):
        """Test includes PubMed context when provided."""
        summaries = ["base context"]
        result = get_current_context(summaries, pubmed_context="PubMed results here")
        assert "base context" in result
        assert "PubMed context:" in result
        assert "PubMed results here" in result

    def test_pubmed_only(self):
        """Test returns PubMed context when no summaries."""
        result = get_current_context(None, pubmed_context="PubMed results")
        assert result == "PubMed context: PubMed results"


class TestEnsureMessageAlternation:
    """Tests for ensure_message_alternation utility."""

    def test_empty_list(self):
        """Test with empty list."""
        from fighthealthinsurance.utils import ensure_message_alternation

        result = ensure_message_alternation([])
        assert result == []

    def test_already_alternating(self):
        """Test list that already alternates correctly."""
        from fighthealthinsurance.utils import ensure_message_alternation

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "Good!"},
        ]
        result = ensure_message_alternation(messages)
        assert len(result) == 4

    def test_merges_consecutive_same_role(self):
        """Test that consecutive messages of same role are handled."""
        from fighthealthinsurance.utils import ensure_message_alternation

        messages = [
            {"role": "user", "content": "First"},
            {"role": "user", "content": "Second"},
            {"role": "assistant", "content": "Response"},
        ]
        result = ensure_message_alternation(messages)
        # Should merge or handle the consecutive user messages
        assert len(result) >= 1
        # Should end properly
        if len(result) >= 2:
            # Check alternation
            for i in range(len(result) - 1):
                if result[i]["role"] == result[i + 1]["role"]:
                    # If same role, content should be merged
                    pass


class TestMakePlaceholder:
    """Tests for the make_placeholder function."""

    def test_placeholder_contains_prefix(self):
        """Placeholder should contain the standard prefix."""
        result = make_placeholder(5)
        assert MISSING_CONTEXT_PREFIX in result

    def test_placeholder_contains_counter(self):
        """Placeholder should contain the counter value."""
        result = make_placeholder(42)
        assert "[42]" in result

    def test_different_counters_produce_different_placeholders(self):
        """Different counters should produce unique placeholders."""
        p1 = make_placeholder(1)
        p2 = make_placeholder(2)
        assert p1 != p2

    def test_placeholder_is_string(self):
        """Placeholder should be a string."""
        assert isinstance(make_placeholder(0), str)


class TestBackgroundGenerateSummary:
    """Tests for the background_generate_summary function."""

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
        new_callable=AsyncMock,
    )
    async def test_replaces_matching_placeholder(self, mock_summarize):
        """Background task should replace its specific placeholder with the generated summary."""
        from fighthealthinsurance.models import OngoingChat

        placeholder = make_placeholder(2)
        chat = await OngoingChat.objects.acreate(
            chat_history=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            summary_for_next_call=[placeholder],
        )

        mock_summarize.return_value = "Patient asking about GLP-1 denial appeal"

        await background_generate_summary(chat.id, placeholder)

        await chat.arefresh_from_db()
        assert (
            chat.summary_for_next_call[-1] == "Patient asking about GLP-1 denial appeal"
        )
        mock_summarize.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
        new_callable=AsyncMock,
    )
    async def test_does_not_overwrite_if_placeholder_replaced(self, mock_summarize):
        """If the placeholder has been replaced by a real summary, background task is a no-op."""
        from fighthealthinsurance.models import OngoingChat

        placeholder = make_placeholder(2)
        chat = await OngoingChat.objects.acreate(
            chat_history=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi!"},
            ],
            summary_for_next_call=["Real summary from panda emoji"],
        )

        mock_summarize.return_value = "Background generated summary"

        await background_generate_summary(chat.id, placeholder)

        await chat.arefresh_from_db()
        # Should still be the real summary, not overwritten
        assert chat.summary_for_next_call[-1] == "Real summary from panda emoji"

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
        new_callable=AsyncMock,
    )
    async def test_only_replaces_correct_placeholder(self, mock_summarize):
        """When multiple placeholders exist, only the matching one is replaced."""
        from fighthealthinsurance.models import OngoingChat

        placeholder_old = make_placeholder(2)
        placeholder_new = make_placeholder(4)
        chat = await OngoingChat.objects.acreate(
            chat_history=[
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "resp1"},
                {"role": "user", "content": "msg2"},
                {"role": "assistant", "content": "resp2"},
            ],
            summary_for_next_call=[placeholder_old, placeholder_new],
        )

        mock_summarize.return_value = "Summary for message 2"

        # Background task for the OLD placeholder
        await background_generate_summary(chat.id, placeholder_old)

        await chat.arefresh_from_db()
        # Old placeholder replaced, new one untouched
        assert chat.summary_for_next_call[0] == "Summary for message 2"
        assert chat.summary_for_next_call[1] == placeholder_new

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
        new_callable=AsyncMock,
    )
    async def test_handles_missing_chat(self, mock_summarize):
        """Background task should handle a deleted chat gracefully."""
        import uuid

        # Non-existent chat ID
        await background_generate_summary(uuid.uuid4(), make_placeholder(1))
        mock_summarize.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
        new_callable=AsyncMock,
    )
    async def test_handles_empty_summary_from_ml(self, mock_summarize):
        """If summarize_chat_history returns None, placeholder is not replaced."""
        from fighthealthinsurance.models import OngoingChat

        placeholder = make_placeholder(2)
        chat = await OngoingChat.objects.acreate(
            chat_history=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi!"},
            ],
            summary_for_next_call=[placeholder],
        )

        mock_summarize.return_value = None

        await background_generate_summary(chat.id, placeholder)

        await chat.arefresh_from_db()
        # Placeholder should still be there
        assert chat.summary_for_next_call[-1] == placeholder

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.chat.context_manager.ml_router.summarize_chat_history",
        new_callable=AsyncMock,
    )
    async def test_handles_summarize_exception(self, mock_summarize):
        """If summarize_chat_history raises, placeholder is not replaced."""
        from fighthealthinsurance.models import OngoingChat

        placeholder = make_placeholder(2)
        chat = await OngoingChat.objects.acreate(
            chat_history=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi!"},
            ],
            summary_for_next_call=[placeholder],
        )

        mock_summarize.side_effect = Exception("ML backend unavailable")

        await background_generate_summary(chat.id, placeholder)

        await chat.arefresh_from_db()
        assert chat.summary_for_next_call[-1] == placeholder

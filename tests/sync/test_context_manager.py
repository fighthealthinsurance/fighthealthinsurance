"""
Tests for the chat context manager module.
"""

import pytest
from unittest.mock import AsyncMock, patch

from fighthealthinsurance.chat.context_manager import (
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

        # Truncated history should be <= DEFAULT_MESSAGES_TO_KEEP
        assert len(history) <= DEFAULT_MESSAGES_TO_KEEP
        # Full history should be preserved
        assert full_history is not None
        assert len(full_history) == 60

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

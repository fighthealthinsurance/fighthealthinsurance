import pytest
import unittest
from fighthealthinsurance.utils import ensure_message_alternation


class TestEnsureMessageAlternation(unittest.TestCase):
    """Tests for ensure_message_alternation utility function."""

    def test_empty_history(self):
        """Empty history should return empty list."""
        result = ensure_message_alternation([])
        self.assertEqual(result, [])

    def test_single_user_message(self):
        """Single user message should be preserved."""
        history = [{"role": "user", "content": "Hello"}]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], "Hello")

    def test_single_assistant_message(self):
        """Single assistant message should be preserved."""
        history = [{"role": "assistant", "content": "Hi there"}]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertEqual(result[0]["content"], "Hi there")

    def test_proper_alternation(self):
        """Properly alternating messages should be unchanged."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I'm fine"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")
        self.assertEqual(result[2]["role"], "user")
        self.assertEqual(result[3]["role"], "assistant")

    def test_consecutive_user_messages_merged(self):
        """Consecutive user messages should be merged."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I'm fine"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertIn("Hello", result[0]["content"])
        self.assertIn("How are you?", result[0]["content"])
        self.assertEqual(result[1]["role"], "assistant")

    def test_consecutive_assistant_messages_merged(self):
        """Consecutive assistant messages should be merged."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "assistant", "content": "How can I help?"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")
        self.assertIn("Hi", result[1]["content"])
        self.assertIn("How can I help?", result[1]["content"])

    def test_agent_role_normalized_to_assistant(self):
        """'agent' role should be normalized to 'assistant'."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "agent", "content": "Hi there"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "assistant")

    def test_system_role_normalized_to_assistant(self):
        """'system' role should be normalized to 'assistant'."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "System message"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "assistant")

    def test_mixed_roles_merged_correctly(self):
        """Mixed agent/assistant/system should all be merged as assistant."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "agent", "content": "I'm an agent"},
            {"role": "system", "content": "System info"},
            {"role": "user", "content": "Thanks"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")
        # All three should be merged into one assistant message
        self.assertIn("Hi", result[1]["content"])
        self.assertIn("I'm an agent", result[1]["content"])
        self.assertIn("System info", result[1]["content"])
        self.assertEqual(result[2]["role"], "user")

    def test_empty_content_skipped(self):
        """Messages with empty content should be skipped."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["content"], "Hi there")

    def test_timestamp_preserved(self):
        """Timestamp should be preserved (most recent when merging)."""
        history = [
            {"role": "user", "content": "Hello", "timestamp": "2024-01-01T10:00:00"},
            {"role": "user", "content": "More", "timestamp": "2024-01-01T10:01:00"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timestamp"], "2024-01-01T10:01:00")

    def test_multiple_consecutive_merges(self):
        """Multiple groups of consecutive same-role messages should all be merged."""
        history = [
            {"role": "user", "content": "A"},
            {"role": "user", "content": "B"},
            {"role": "user", "content": "C"},
            {"role": "assistant", "content": "1"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "D"},
            {"role": "user", "content": "E"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 3)
        self.assertIn("A", result[0]["content"])
        self.assertIn("B", result[0]["content"])
        self.assertIn("C", result[0]["content"])
        self.assertIn("1", result[1]["content"])
        self.assertIn("2", result[1]["content"])
        self.assertIn("D", result[2]["content"])
        self.assertIn("E", result[2]["content"])


if __name__ == "__main__":
    unittest.main()

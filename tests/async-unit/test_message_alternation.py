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

    def test_system_role_normalized_left_as_is(self):
        """'system' role should be left as 'system'."""
        history = [
            {"role": "system", "content": "System message"},
            {"role": "user", "content": "Hello"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "system")

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

    def test_system_followed_by_assistant_removes_assistant(self):
        """System followed by assistant should remove the assistant."""
        history = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Hi"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[1]["role"], "user")

    def test_system_followed_by_assistant_only(self):
        """System followed by assistant only should return just system."""
        history = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "system")

    def test_leading_assistant_removed(self):
        """Leading assistant message should be removed."""
        history = [
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "How can I help?"},
        ]
        result = ensure_message_alternation(history)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")

    def test_system_assistant_system_user_merges_systems(self):
        """Removing assistant between systems should merge the systems."""
        history = [
            {"role": "system", "content": "System prompt 1"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "system", "content": "System prompt 2"},
            {"role": "user", "content": "Hi"},
        ]
        result = ensure_message_alternation(history)
        # After removing assistant, systems should be merged
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "system")
        self.assertIn("System prompt 1", result[0]["content"])
        self.assertIn("System prompt 2", result[0]["content"])
        self.assertEqual(result[1]["role"], "user")

    def test_multiple_leading_assistants_removed(self):
        """Multiple leading assistant messages should all be removed."""
        history = [
            {"role": "assistant", "content": "First"},
            {"role": "assistant", "content": "Second"},
            {"role": "user", "content": "Hi"},
        ]
        result = ensure_message_alternation(history)
        # First the assistants are merged, then the leading assistant is removed
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")

    def test_system_then_agent_never_occurs(self):
        """System followed by agent should never occur in output."""
        # This tests the specific bug: system then agent should never happen
        history = [
            {"role": "system", "content": "System"},
            {"role": "agent", "content": "Agent response"},
            {"role": "user", "content": "User message"},
        ]
        result = ensure_message_alternation(history)
        # Verify system is never immediately followed by assistant/agent
        for i in range(len(result) - 1):
            if result[i]["role"] == "system":
                self.assertNotEqual(
                    result[i + 1]["role"],
                    "assistant",
                    "System should never be followed by assistant",
                )


if __name__ == "__main__":
    unittest.main()

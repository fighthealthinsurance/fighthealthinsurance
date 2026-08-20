"""Bounds and flags in the transactional chat persistence helpers."""

from django.test import TestCase

from fighthealthinsurance.chat.chat_persistence import (
    MAX_STORED_SUMMARIES,
    merge_new_messages,
)


class MergeNewMessagesInternalFlagTest(TestCase):
    def test_internal_flag_is_preserved(self):
        history = merge_new_messages(
            [],
            [
                {
                    "role": "user",
                    "content": "Linked this chat to Appeal #4 -- details",
                    "internal": True,
                },
                {"role": "assistant", "content": "I've linked this chat."},
            ],
        )
        self.assertTrue(history[0].get("internal"))
        self.assertNotIn("internal", history[1])

    def test_normal_messages_have_no_internal_key(self):
        history = merge_new_messages([], [{"role": "user", "content": "hello there"}])
        self.assertNotIn("internal", history[0])

    def test_tail_dedup_still_applies(self):
        history = [{"role": "user", "content": "CA", "timestamp": "t"}]
        merged = merge_new_messages(history, [{"role": "user", "content": "CA"}])
        self.assertEqual(len(merged), 1)


class SummaryCapTest(TestCase):
    def _persist(self, chat_id, summaries):
        from fighthealthinsurance.chat.chat_persistence import (
            _persist_chat_turn_sync,
        )

        return _persist_chat_turn_sync(chat_id, [], summaries)

    def test_summary_list_is_capped(self):
        from fighthealthinsurance.models import OngoingChat

        chat = OngoingChat.objects.create(chat_history=[], summary_for_next_call=[])
        for i in range(MAX_STORED_SUMMARIES + 15):
            self._persist(chat.id, [f"summary {i}"])
        chat.refresh_from_db()
        self.assertEqual(len(chat.summary_for_next_call), MAX_STORED_SUMMARIES)
        # The most recent summaries are the ones kept.
        self.assertEqual(
            chat.summary_for_next_call[-1],
            f"summary {MAX_STORED_SUMMARIES + 14}",
        )

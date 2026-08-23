"""Bounds and flags in the transactional chat persistence helpers."""

from django.test import TestCase

from fighthealthinsurance.chat.chat_persistence import (
    MAX_STORED_SUMMARIES,
    is_internal_history_message,
    merge_new_messages,
    visible_history,
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

    def test_legacy_note_with_uuid_id_is_internal(self):
        """PriorAuthRequest pks are UUIDs (Appeal pks are ints); the legacy
        pattern's `#\\d+` silently missed every un-flagged prior-auth note
        whose UUID starts with a hex letter, rendering internal system text
        as a user bubble and letting previews/titles pick it up."""
        for note in (
            "Linked this chat to Prior Auth Request "
            "#f3a91c2e-1d5b-4e6f-9a3c-2b7d8e4f5a6b, details are ...",
            "This chat is already linked to Prior Auth Request "
            "#03a91c2e-1d5b-4e6f-9a3c-2b7d8e4f5a6b, current details are ...",
            "Linked this chat to Appeal #12 -- details",
        ):
            with self.subTest(note=note):
                self.assertTrue(
                    is_internal_history_message({"role": "user", "content": note})
                )
        # A user's own message about their prior auth still is NOT internal.
        self.assertFalse(
            is_internal_history_message(
                {"role": "user", "content": "I linked my prior auth already"}
            )
        )

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


class MergeInternalNoteAfterUserTailTest(TestCase):
    """A failed turn leaves a user-role tail, so the next internal note hits
    the consecutive-user merge branch. That branch returned before the
    internal flag was applied, producing an unflagged message whose text no
    longer matched a known internal prefix — so replay rendered the system
    note inside the user's own bubble."""

    def test_internal_note_is_not_merged_into_a_user_message(self):
        history = [{"role": "user", "content": "what about my knee MRI?"}]
        merged = merge_new_messages(
            history,
            [
                {
                    "role": "user",
                    "content": "Linked this chat to Appeal #4 -- help the user iterate",
                    "internal": True,
                }
            ],
        )

        self.assertEqual(len(merged), 2, f"internal note was merged away: {merged}")
        self.assertTrue(merged[-1].get("internal"))
        self.assertEqual(merged[0]["content"], "what about my knee MRI?")

    def test_internal_note_is_hidden_from_replay(self):
        history = merge_new_messages(
            [{"role": "user", "content": "what about my knee MRI?"}],
            [
                {
                    "role": "user",
                    "content": "Linked this chat to Appeal #4 -- help the user iterate",
                    "internal": True,
                }
            ],
        )
        visible = visible_history(history)
        self.assertEqual([m["content"] for m in visible], ["what about my knee MRI?"])

    def test_consecutive_plain_user_messages_still_merge(self):
        merged = merge_new_messages(
            [{"role": "user", "content": "first"}],
            [{"role": "user", "content": "second"}],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["content"], "first second")


class InternalHistoryDetectionTest(TestCase):
    def test_flagged_message_is_internal(self):
        self.assertTrue(
            is_internal_history_message(
                {"role": "user", "content": "x", "internal": True}
            )
        )

    def test_legacy_generated_note_is_internal(self):
        self.assertTrue(
            is_internal_history_message(
                {
                    "role": "user",
                    "content": "Linked this chat to Appeal #12 -- help the user iterate",
                }
            )
        )

    def test_user_text_that_merely_starts_with_the_prefix_is_visible(self):
        """The bare prefix is text a user can type; matching it hid their own
        message from their own replay while every server-side view kept it."""
        self.assertFalse(
            is_internal_history_message(
                {
                    "role": "user",
                    "content": (
                        "Linked this chat to my other appeal — here is the "
                        "member ID 12345, can you compare them?"
                    ),
                }
            )
        )

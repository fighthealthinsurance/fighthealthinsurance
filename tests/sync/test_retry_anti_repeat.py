"""Tests for the anti-repeat retry ladder in the chat retry handler."""

import pytest

from fighthealthinsurance.chat.retry_handler import (
    ANTI_REPEAT_NOTE,
    ANTI_REPEAT_TEMPERATURE,
    REPEAT_LAST_RESORT_PENALTY,
    create_simple_retry_scorer,
    retry_llm_with_fallback,
)
from tests.chat_fixtures import FRESH_REPLY, LOOPED_REPLY

CHAT_HISTORY = [
    {"role": "user", "content": "Help me with the new medicaid requirements."},
    {"role": "assistant", "content": LOOPED_REPLY},
]


class RecordingModel:
    """Minimal chat backend that records generate_chat_response calls."""

    def __init__(self, response=FRESH_REPLY, context="ctx"):
        self.calls = []
        self._response = response
        self._context = context

    def quality(self):
        return 100

    async def generate_chat_response(
        self,
        current_message_for_llm,
        previous_context_summary=None,
        history=None,
        is_professional=True,
        is_logged_in=True,
        temperature=0.7,
        allow_repeated_reply=False,
    ):
        self.calls.append(
            {
                "message": current_message_for_llm,
                "temperature": temperature,
                "history_len": len(history) if history else 0,
                "allow_repeated_reply": allow_repeated_reply,
            }
        )
        return (self._response, self._context)


class TestRetryScorerRepeatPenalty:
    """A retry candidate that still repeats loses to any fresh candidate but
    stays deliverable as the absolute last resort (finite score, not -inf)."""

    def _scorer(self):
        return create_simple_retry_scorer(
            call_scores={},
            chat_history=CHAT_HISTORY,
            current_message="CA",
        )

    def test_repeat_scores_decisively_below_fresh(self):
        scorer = self._scorer()
        repeat_score = scorer((LOOPED_REPLY, "ctx"), None)
        fresh_score = scorer((FRESH_REPLY, "ctx"), None)
        assert repeat_score < fresh_score + REPEAT_LAST_RESORT_PENALTY / 2
        assert repeat_score > float("-inf")

    def test_repeat_allowed_when_user_asked_for_it(self):
        scorer = create_simple_retry_scorer(
            call_scores={},
            chat_history=CHAT_HISTORY,
            current_message="can you repeat that?",
        )
        repeat_score = scorer((LOOPED_REPLY, "ctx"), None)
        assert repeat_score > REPEAT_LAST_RESORT_PENALTY / 2


class TestRetryAntiRepeatInstruction:
    """retry_llm_with_fallback(anti_repeat=True) changes what the model sees."""

    @pytest.mark.asyncio
    async def test_anti_repeat_adds_note_and_raises_temperature(self):
        model = RecordingModel()
        response, context = await retry_llm_with_fallback(
            model_backends=[model],
            current_message="CA",
            previous_context_summary=None,
            history=list(CHAT_HISTORY),
            is_professional=False,
            is_logged_in=False,
            chat_history=CHAT_HISTORY,
            user_message_for_scoring="CA",
            anti_repeat=True,
            timeout=5.0,
        )
        assert response == FRESH_REPLY
        assert model.calls
        for call in model.calls:
            assert ANTI_REPEAT_NOTE in call["message"]
            assert call["temperature"] == ANTI_REPEAT_TEMPERATURE

    @pytest.mark.asyncio
    async def test_default_retry_has_no_note(self):
        model = RecordingModel()
        response, _ = await retry_llm_with_fallback(
            model_backends=[model],
            current_message="CA",
            previous_context_summary=None,
            history=list(CHAT_HISTORY),
            is_professional=False,
            is_logged_in=False,
            chat_history=CHAT_HISTORY,
            user_message_for_scoring="CA",
            timeout=5.0,
        )
        assert response == FRESH_REPLY
        for call in model.calls:
            assert ANTI_REPEAT_NOTE not in call["message"]
            assert call["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_allow_repeated_reply_forwards_to_backends(self):
        """The explicit-repeat flag (derived from the RAW user message by the
        caller) must reach the backend so its self-heal loop doesn't fight a
        user-requested repeat."""
        model = RecordingModel()
        await retry_llm_with_fallback(
            model_backends=[model],
            current_message="please repeat that",
            previous_context_summary=None,
            history=list(CHAT_HISTORY),
            is_professional=False,
            is_logged_in=False,
            chat_history=CHAT_HISTORY,
            user_message_for_scoring="please repeat that",
            allow_repeated_reply=True,
            timeout=5.0,
        )
        assert model.calls
        for call in model.calls:
            assert call["allow_repeated_reply"] is True

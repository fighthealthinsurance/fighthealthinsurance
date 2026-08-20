"""End-to-end chat tests for the loop-prevention ladder.

Covers the observed production failure: the model re-sends its previous
reply verbatim after the user answers its question ("CA"). The ladder is:
hard rejection of repeated candidates in scoring -> anti-repeat retry with
an explicit instruction and hotter sampling -> delivery of a fresh reply.
Also covers the side-by-side alternate answer, the transient IP-derived
state hint, and the LLM-input debug frame.
"""

import typing
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from prometheus_client import REGISTRY
from rest_framework.test import APITestCase

from fighthealthinsurance.chat.retry_handler import ANTI_REPEAT_NOTE
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import OngoingChat, ProfessionalUser

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


LOOPED_REPLY = (
    "The new Medicaid requirements can be tricky! To help you understand them "
    "better, could you tell me a bit more about your situation? For example:\n\n"
    "* What state are you in?\n"
    "* What is your current income and household size?\n"
    "* Do you currently have Medicaid coverage?\n"
    "* What specific requirements are you trying to understand?\n\n"
    "Once I have this information, I can help you find the relevant resources "
    "and explain how to navigate the new requirements."
)

FRESH_REPLY = (
    "Great — since you're in California, your Medicaid program is Medi-Cal. "
    "The new federal rules add an 80-hour monthly work, school, or "
    "volunteering requirement for many adults by the end of 2026. Want me to "
    "look up the Medi-Cal specifics for you?"
)

SECOND_OPINION_REPLY = (
    "One way to start: call the Medi-Cal member help line and ask which of "
    "the new requirements apply to your household, and keep records of any "
    "qualifying hours. I can also walk you through the rules here if you "
    "prefer."
)


class _FrameRecorder:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)

    def content_frames(self):
        return [f for f in self.frames if "content" in f]

    def debug_frames(self):
        return [f for f in self.frames if "debug_llm_input" in f]

    def debug_result_frames(self):
        return [f for f in self.frames if "debug_llm_result" in f]


class RecordingChatModel:
    """Chat backend stub: records calls; loops until told not to repeat.

    Returns ``looped_reply`` for every call whose message does NOT carry the
    anti-repeat instruction, and ``fresh_reply`` once it does — mimicking a
    backend that re-emits its previous reply until explicitly steered.
    """

    def __init__(
        self,
        looped_reply=LOOPED_REPLY,
        fresh_reply=FRESH_REPLY,
        always_reply=None,
        model_quality=100,
        name="recording-model",
    ):
        self.calls = []
        self._looped_reply = looped_reply
        self._fresh_reply = fresh_reply
        self._always_reply = always_reply
        self._quality = model_quality
        self._name = name

    def __str__(self):
        return self._name

    def quality(self):
        return self._quality

    def get_max_context(self) -> int:
        return 32768

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
                "context": previous_context_summary,
                "allow_repeated_reply": allow_repeated_reply,
                "history": list(history) if history else [],
                "temperature": temperature,
            }
        )
        if self._always_reply is not None:
            return (self._always_reply, "mock context summary")
        if ANTI_REPEAT_NOTE in (current_message_for_llm or ""):
            return (self._fresh_reply, "mock context summary")
        return (self._looped_reply, "mock context summary")


def _metric(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _make_chat(username, npi, chat_history=None):
    user = await sync_to_async(User.objects.create_user)(
        username=username, password="testpass", email=f"{username}@example.com"
    )
    professional = await ProfessionalUser.objects.acreate(
        user=user, active=True, npi_number=npi
    )
    chat = await OngoingChat.objects.acreate(
        professional_user=professional,
        chat_history=chat_history or [],
        summary_for_next_call=[],
    )
    return user, chat


def _seed_history():
    return [
        {"role": "user", "content": "Help me with the new medicaid requirements."},
        {"role": "assistant", "content": LOOPED_REPLY},
    ]


def _patched_router(models, fallback=None):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
        return_value=(models, fallback or []),
    )


_PATCH_FIRE_AND_FORGET = patch(
    "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
    new_callable=AsyncMock,
)


class ChatRepeatRejectionTest(APITestCase):
    async def test_looping_backend_is_broken_by_anti_repeat_retry(self):
        """A backend that re-sends its previous reply gets rejected, retried
        with the anti-repeat instruction, and the fresh reply is delivered."""
        user, chat = await _make_chat(
            "loopbreak1", "9999930001", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel()

        rejected_before = _metric(
            "fhi_chat_repeated_responses_total", {"action": "rejected_candidates"}
        )

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        content_frames = recorder.content_frames()
        assert content_frames, f"expected a reply frame, got: {recorder.frames}"
        delivered = content_frames[-1]["content"]
        assert delivered == FRESH_REPLY, f"delivered the looped reply: {delivered:.80}"

        # The retry actually carried the anti-repeat instruction.
        assert any(ANTI_REPEAT_NOTE in c["message"] for c in model.calls)
        # And the rejection was recorded for observability.
        assert (
            _metric(
                "fhi_chat_repeated_responses_total",
                {"action": "rejected_candidates"},
            )
            == rejected_before + 1
        )

        # The delivered (fresh) turn was persisted, not the looped one.
        fresh = await OngoingChat.objects.aget(id=chat.id)
        assistant_msgs = [m for m in fresh.chat_history if m.get("role") == "assistant"]
        assert assistant_msgs[-1]["content"] == FRESH_REPLY

    async def test_terse_reply_gets_bridge_note(self):
        """A short answer right after an assistant question carries the
        system bridge note into the model call."""
        user, chat = await _make_chat(
            "loopbreak2", "9999930002", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        assert model.calls
        assert any(
            "this short reply answers the question" in c["message"] for c in model.calls
        ), f"no bridge note in: {model.calls[0]['message'][:400]}"

    async def test_long_message_gets_no_bridge_note(self):
        """A full-sentence reply doesn't need (or get) the bridge note."""
        user, chat = await _make_chat(
            "loopbreak3", "9999930003", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        long_message = (
            "I'm in California, my household is two people, our income is "
            "about $2,900 a month, and I currently have Medi-Cal coverage."
        )
        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message(long_message)

        assert model.calls
        assert not any(
            "this short reply answers the question" in c["message"] for c in model.calls
        )


class ChatAlternateAnswerTest(APITestCase):
    async def test_distinct_runner_up_offered_as_alternate(self):
        user, chat = await _make_chat(
            "alternate1", "9999930004", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        best_model = RecordingChatModel(always_reply=FRESH_REPLY, model_quality=110)
        second_model = RecordingChatModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=100
        )

        offered_before = _metric("fhi_chat_alternate_answers_total")

        with _patched_router([best_model, second_model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        content_frames = recorder.content_frames()
        assert content_frames
        frame = content_frames[-1]
        assert frame["content"] == FRESH_REPLY
        assert frame.get("alternate_content") == SECOND_OPINION_REPLY
        assert _metric("fhi_chat_alternate_answers_total") == offered_before + 1

        # Only the primary reply is persisted.
        fresh = await OngoingChat.objects.aget(id=chat.id)
        contents = [m.get("content") for m in fresh.chat_history]
        assert FRESH_REPLY in contents
        assert SECOND_OPINION_REPLY not in contents

    async def test_near_duplicate_runner_up_not_offered(self):
        user, chat = await _make_chat(
            "alternate2", "9999930005", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        best_model = RecordingChatModel(always_reply=FRESH_REPLY, model_quality=110)
        near_dup_model = RecordingChatModel(
            always_reply=FRESH_REPLY.replace("Great —", "Good news:"),
            model_quality=100,
        )

        with _patched_router([best_model, near_dup_model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        content_frames = recorder.content_frames()
        assert content_frames
        assert "alternate_content" not in content_frames[-1]

    async def test_wide_score_gap_runner_up_not_offered(self):
        """A runner-up far below the winner (cross-tier quality gap) is not
        worth the user's attention: alternates only show on close ties."""
        user, chat = await _make_chat(
            "alternate3", "9999930015", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        strong_model = RecordingChatModel(
            always_reply=FRESH_REPLY, model_quality=200, name="strong-internal"
        )
        weak_model = RecordingChatModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=100, name="weak-external"
        )

        with _patched_router([strong_model, weak_model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        content_frames = recorder.content_frames()
        assert content_frames
        frame = content_frames[-1]
        assert frame["content"] == FRESH_REPLY
        assert "alternate_content" not in frame


class ChatRepeatOffenderTest(APITestCase):
    async def test_looping_backend_gets_session_strikes(self):
        """A backend whose candidate is hard-rejected as a repeat collects a
        per-session strike (used to decay its base score on later turns),
        and the fresh answer from the other backend is delivered."""
        user, chat = await _make_chat(
            "offender1", "9999930016", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        looper = RecordingChatModel(
            always_reply=LOOPED_REPLY, model_quality=110, name="looping-backend"
        )
        fresh = RecordingChatModel(
            always_reply=FRESH_REPLY, model_quality=100, name="fresh-backend"
        )

        with _patched_router([looper, fresh]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        content_frames = recorder.content_frames()
        assert content_frames
        assert content_frames[-1]["content"] == FRESH_REPLY
        assert interface._repeat_offenders.get("looping-backend", 0) >= 1
        assert "fresh-backend" not in interface._repeat_offenders


class ChatStateHintTest(APITestCase):
    async def test_state_hint_reaches_model_context_as_unconfirmed(self):
        user, chat = await _make_chat(
            "statehint1", "9999930006", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
            state_hint="California",
        )
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("Do the new rules apply to me?")

        assert model.calls
        context = model.calls[0]["context"] or ""
        assert "California" in context
        assert "UNCONFIRMED" in context

        # The hint is transient: nothing about it is persisted on the chat.
        fresh = await OngoingChat.objects.aget(id=chat.id)
        persisted_blob = str(fresh.chat_history) + str(fresh.summary_for_next_call)
        assert "UNCONFIRMED guess" not in persisted_blob

    async def test_no_hint_no_context_injection(self):
        user, chat = await _make_chat(
            "statehint2", "9999930007", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("Do the new rules apply to me?")

        assert model.calls
        context = model.calls[0]["context"] or ""
        assert "UNCONFIRMED" not in context


class ChatDebugFrameTest(APITestCase):
    async def test_debug_frame_shows_exact_llm_input(self):
        user, chat = await _make_chat(
            "chatdebug1", "9999930008", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
            debug_llm=True,
        )
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        debug_frames = recorder.debug_frames()
        assert debug_frames, f"expected a debug frame, got: {recorder.frames}"
        debug = debug_frames[0]["debug_llm_input"]
        # The frame carries the exact wrapped message the model received.
        assert model.calls
        assert debug["message_for_llm"] == model.calls[0]["message"]
        assert "CA" in debug["message_for_llm"]
        assert debug["history_message_count"] == 2

    async def test_no_debug_frame_by_default(self):
        user, chat = await _make_chat(
            "chatdebug2", "9999930009", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        assert not recorder.debug_frames()
        assert not recorder.debug_result_frames()

    async def test_debug_result_frame_reports_model_selection(self):
        """With debug on, each turn also reports WHICH backend won, both
        scores, and the anti-loop bookkeeping — the fan-out is otherwise a
        black box when triaging a bad reply."""
        user, chat = await _make_chat(
            "chatdebug3", "9999930017", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
            debug_llm=True,
        )
        best_model = RecordingChatModel(
            always_reply=FRESH_REPLY, model_quality=110, name="winner-model"
        )
        second_model = RecordingChatModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=100, name="second-model"
        )

        with _patched_router([best_model, second_model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        result_frames = recorder.debug_result_frames()
        assert result_frames, f"expected a debug result frame: {recorder.frames}"
        result = result_frames[0]["debug_llm_result"]
        assert result["picked_model"] == "winner-model"
        assert result["runner_up_model"] == "second-model"
        assert result["picked_score"] > result["runner_up_score"]
        assert result["closely_tied"] is True
        assert result["alternate_offered"] is True
        assert result["candidate_count"] == 2
        assert result["rejected_repeats"] == 0
        assert result["retry_used"] is False
        assert result["elapsed_ms"] >= 0
        assert {c["model"] for c in result["scored_candidates"]} == {
            "winner-model",
            "second-model",
        }


class ChatDisconnectSafetyTest(APITestCase):
    async def test_user_message_survives_mid_turn_cancellation(self):
        """Channels cancels the consumer's coroutine on disconnect; the
        user's message must already be persisted by then (persistence used
        to happen only after generation, so a network blip erased the
        turn)."""
        import asyncio

        user, chat = await _make_chat(
            "cancelmid1", "9999930010", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)

        with (
            _patched_router([RecordingChatModel(always_reply=FRESH_REPLY)]),
            _PATCH_FIRE_AND_FORGET,
            patch.object(
                ChatInterface,
                "_call_llm_with_actions",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
        ):
            try:
                await interface.handle_chat_message("CA")
            except asyncio.CancelledError:
                pass

        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_msgs = [
            m["content"] for m in fresh.chat_history if m.get("role") == "user"
        ]
        assert "CA" in user_msgs, f"user message lost: {fresh.chat_history}"

    async def test_completed_turn_has_no_duplicate_user_message(self):
        """The early persist plus the end-of-turn persist must not double
        the user's message (tail-dedup in the merge helper)."""
        user, chat = await _make_chat(
            "cancelmid2", "9999930011", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)

        with (
            _patched_router([RecordingChatModel(always_reply=FRESH_REPLY)]),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("CA")

        fresh = await OngoingChat.objects.aget(id=chat.id)
        ca_msgs = [
            m
            for m in fresh.chat_history
            if m.get("role") == "user" and m.get("content") == "CA"
        ]
        assert len(ca_msgs) == 1, f"duplicated user message: {fresh.chat_history}"
        assert fresh.chat_history[-1]["content"] == FRESH_REPLY


class ChatReplayFilterTest(APITestCase):
    async def test_internal_link_messages_do_not_replay(self):
        """Appeal-link notes are stored as role=user for LLM context; they
        must not replay as bubbles the user never typed."""
        seeded = _seed_history() + [
            {
                "role": "user",
                "content": "Linked this chat to Appeal #4 -- help the user iterate",
                "internal": True,
            },
            {"role": "assistant", "content": "I've linked this chat to your appeal."},
            # Legacy entry from before the internal flag existed.
            {
                "role": "user",
                "content": "This chat is already linked to Appeal #4 -- details",
            },
        ]
        user, chat = await _make_chat("replayfilter1", "9999930012", seeded)
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)

        await interface.replay_chat_history()

        replay_frames = [f for f in recorder.frames if "messages" in f]
        assert replay_frames
        contents = [m["content"] for m in replay_frames[0]["messages"]]
        assert "I've linked this chat to your appeal." in contents
        assert not any(c.startswith("Linked this chat to ") for c in contents)
        assert not any(
            c.startswith("This chat is already linked to ") for c in contents
        )
        # Normal user messages still replay.
        assert "Help me with the new medicaid requirements." in contents


class ChatConcurrentPrePersistTest(APITestCase):
    async def test_concurrent_prepersist_does_not_duplicate_user_message(self):
        """Two connections racing on one chat: the second turn's pre-persist
        merges into this turn's pending user message ("CA" -> "CA B"). The
        end-of-turn persist must not resubmit this turn's user message, or
        it would append AGAIN against the merged tail ("CA B CA")."""
        from fighthealthinsurance.chat.chat_persistence import apersist_chat_turn

        user, chat = await _make_chat(
            "raceturn1", "9999930013", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)

        class RacingModel(RecordingChatModel):
            """Simulates the other connection's pre-persist landing while
            this turn is mid-generation."""

            async def generate_chat_response(self, *args, **kwargs):
                await apersist_chat_turn(
                    chat, new_messages=[{"role": "user", "content": "B"}]
                )
                return await super().generate_chat_response(*args, **kwargs)

        model = RacingModel(always_reply=FRESH_REPLY)
        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        fresh = await OngoingChat.objects.aget(id=chat.id)
        user_contents = [
            m["content"] for m in fresh.chat_history if m.get("role") == "user"
        ]
        # The racing message merged into this turn's pending user message,
        # and this turn's text appears exactly once (no "CA B CA").
        assert "CA B" in user_contents, f"history: {fresh.chat_history}"
        assert not any(
            c.count("CA") > 1 for c in user_contents
        ), f"duplicated user text: {user_contents}"
        assert fresh.chat_history[-1]["content"] == FRESH_REPLY

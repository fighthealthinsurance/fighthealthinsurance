"""End-to-end chat tests for the loop-prevention ladder.

Covers the observed production failure: the model re-sends its previous
reply verbatim after the user answers its question ("CA"). The ladder is:
hard rejection of repeated candidates in scoring -> anti-repeat retry with
an explicit instruction and hotter sampling -> delivery of a fresh reply.
Also covers the side-by-side alternate answer, the transient IP-derived
state hint, and the LLM-input debug frame.
"""

import asyncio
import json
import os
import typing
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import override_settings
from prometheus_client import REGISTRY
from rest_framework.test import APITestCase

from fighthealthinsurance import chat_interface as chat_interface_module
from fighthealthinsurance import utils as fhi_utils
from fighthealthinsurance.chat.retry_handler import ANTI_REPEAT_NOTE
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import ChatTurn, OngoingChat, ProfessionalUser
from tests.chat_fixtures import (
    FRESH_REPLY,
    LOOPED_REPLY,
    SECOND_OPINION_REPLY,
    RecordingChatModel,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


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


def _metric(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _make_chat(username, npi, chat_history=None):
    user = await User.objects.acreate_user(
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

    async def test_metric_recorded_when_the_turn_delivers_nothing(self):
        """The loudest loop is the one that never produces a reply.

        Every candidate rejected as a repeat AND the retry delivering nothing
        returns early, before tool processing -- so the once-per-turn metric
        used to be skipped in exactly the case it exists to alert on,
        reporting "loops never happen" while a backend looped itself dry.
        """
        user, chat = await _make_chat(
            "loopbreak_nodeliver", "9999930010", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        # Primary pass loops (hard-rejected); the anti-repeat retry then comes
        # back empty, so no candidate is deliverable and the turn returns
        # early -- the shape the metric used to miss.
        model = RecordingChatModel(looped_reply=LOOPED_REPLY, fresh_reply="")

        rejected_before = _metric(
            "fhi_chat_repeated_responses_total", {"action": "rejected_candidates"}
        )

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        delivered = [f["content"] for f in recorder.content_frames()]
        assert LOOPED_REPLY not in delivered, f"delivered the looped reply: {delivered}"
        assert (
            _metric(
                "fhi_chat_repeated_responses_total",
                {"action": "rejected_candidates"},
            )
            == rejected_before + 1
        )

    async def test_metric_recorded_when_tool_processing_raises(self):
        """A raising tool handler unwinds past the in-method exits.

        The turn still rejected repeats, so the loop metric must survive the
        exception -- handle_chat_message's finally is the backstop.
        """
        user, chat = await _make_chat(
            "loopbreak_toolraise", "9999930011", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel()

        rejected_before = _metric(
            "fhi_chat_repeated_responses_total", {"action": "rejected_candidates"}
        )

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET, patch(
            "fighthealthinsurance.chat_interface.AppealTool.handle",
            side_effect=RuntimeError("tool blew up"),
        ):
            await interface.handle_chat_message("CA")

        assert (
            _metric(
                "fhi_chat_repeated_responses_total",
                {"action": "rejected_candidates"},
            )
            == rejected_before + 1
        )

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


# A third distinct, presentable answer for the model-versus-model tests.
THIRD_REPLY = (
    "One more option to consider: ask your county office for the hardship "
    "exemption form, since caregivers and people with a medical condition "
    "can be excused from the hours rule entirely."
)

_real_best_two = fhi_utils.best_two_within_timelimit


async def _short_race(calls, score_fn, timeout, extended_timeout=None):
    """The primary race with a half-second window, so a stalled backend is
    left running (late) without the test waiting the real 30s."""
    return await _real_best_two(calls, score_fn, timeout=0.5, extended_timeout=0.0)


_PATCH_SHORT_RACE = patch(
    "fighthealthinsurance.chat_interface.best_two_within_timelimit", _short_race
)


class _RaisingModel(RecordingChatModel):
    async def generate_chat_response(self, *args, **kwargs):
        raise RuntimeError("Blue Shield denied the knee MRI")


class _StalledModel(RecordingChatModel):
    async def generate_chat_response(self, *args, **kwargs):
        await asyncio.sleep(10)
        return ("never delivered", "never delivered")


# The medicaid_info tool's follow-up prompt starts with this.
_MEDICAID_FOLLOW_UP = "official Medicaid information"


class _ToolAskingModel(RecordingChatModel):
    """Asks for the medicaid_info tool, and has nothing for its follow-up."""

    external = False

    async def generate_chat_response(self, current_message_for_llm, **kwargs):
        await super().generate_chat_response(current_message_for_llm, **kwargs)
        if _MEDICAID_FOLLOW_UP in (current_message_for_llm or ""):
            return ("", None)
        return ('**medicaid_info {"state": "California"}**', "mock context summary")


class _FollowUpModel(RecordingChatModel):
    """An outside model with nothing for the first race that writes the
    tool's follow-up."""

    external = True

    async def generate_chat_response(self, current_message_for_llm, **kwargs):
        await super().generate_chat_response(current_message_for_llm, **kwargs)
        if _MEDICAID_FOLLOW_UP in (current_message_for_llm or ""):
            return (FRESH_REPLY, "mock context summary")
        return ("", None)


def _patch_reply_save_to_fail():
    """Saving the user's message works; saving the reply raises."""
    real = chat_interface_module.apersist_chat_turn

    async def persist(chat, new_messages=(), **kwargs):
        if any(m.get("role") == "assistant" for m in new_messages):
            raise OperationalError("the database went away")
        return await real(chat, new_messages=new_messages, **kwargs)

    return patch.object(chat_interface_module, "apersist_chat_turn", persist)


async def _turn_rows(chat):
    return [t async for t in ChatTurn.objects.filter(chat=chat).order_by("created_at")]


def _row_blob(row):
    """Every stored value of a ChatTurn row, as one string."""
    return json.dumps(
        {f.name: getattr(row, f.attname) for f in ChatTurn._meta.concrete_fields},
        default=str,
        # Unescaped, so text with non-ASCII characters is still found.
        ensure_ascii=False,
    )


class _TurnVaryingModel(RecordingChatModel):
    """Answers each turn with a wholly different reply of its own, so no
    turn is rejected as a repeat of an earlier one."""

    def __init__(self, replies, **kwargs):
        super().__init__(always_reply=replies[0], **kwargs)
        self._replies = replies

    async def generate_chat_response(self, *args, **kwargs):
        # One call per turn in these tests (no retries, no tool passes).
        turn = min(len(self.calls), len(self._replies) - 1)
        self._always_reply = self._replies[turn]
        return await super().generate_chat_response(*args, **kwargs)


_WINNER_REPLIES = [
    FRESH_REPLY,
    "In Texas, Medicaid renewals arrive by mail about sixty days before your "
    "coverage ends, so watch for that envelope and answer by its due date. "
    "Want me to walk through the form?",
    "New York lets you renew Medicaid online through NY State of Health, and "
    "most people only confirm their income. Shall I list the documents?",
]
_SECOND_REPLIES = [
    SECOND_OPINION_REPLY,
    "Another way to look at Texas: call 2-1-1 and ask for the renewal packet "
    "status, which tells you whether anything is missing. Want the number?",
    "For New York, a navigator at a local clinic can file the renewal with "
    "you for free. Would a list of nearby navigators help?",
]


class ChatTurnRecordTest(APITestCase):
    """Each model turn leaves one ChatTurn row: metadata about the race,
    never any text."""

    async def test_a_chat_gets_at_most_two_side_by_sides(self):
        """Three close calls in one chat: the first two offer a side-by-side,
        the third does not, and a new socket for the same chat counts the
        earlier ones from the turn rows."""
        user, chat = await _make_chat(
            "sidebyside1", "9999930131", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        best_model = _TurnVaryingModel(
            _WINNER_REPLIES, model_quality=110, name="winner-model"
        )
        second_model = _TurnVaryingModel(
            _SECOND_REPLIES, model_quality=100, name="second-model"
        )
        offered = []
        with _patched_router([best_model, second_model]), _PATCH_FIRE_AND_FORGET:
            for message in ("CA", "TX"):
                interface = ChatInterface(
                    send_json_message_func=recorder, chat=chat, user=user
                )
                await interface.handle_chat_message(message)
                offered.append("alternate_content" in recorder.content_frames()[-1])
            # A fresh socket (reconnect) for the same chat.
            interface = ChatInterface(
                send_json_message_func=recorder, chat=chat, user=user
            )
            await interface.handle_chat_message("NY")
            offered.append("alternate_content" in recorder.content_frames()[-1])
        assert offered == [True, True, False]
        rows = await _turn_rows(chat)
        assert sum(1 for row in rows if row.alternate_offered) == 2

    async def test_two_open_sockets_share_the_limit(self):
        """Socket A offers one, socket B offers one, then A must not offer a
        third on a count it had cached."""
        user, chat = await _make_chat(
            "sidebyside3", "9999930133", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        best_model = _TurnVaryingModel(
            _WINNER_REPLIES, model_quality=110, name="winner-model"
        )
        second_model = _TurnVaryingModel(
            _SECOND_REPLIES, model_quality=100, name="second-model"
        )
        socket_a = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        socket_b = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        offered = []
        with _patched_router([best_model, second_model]), _PATCH_FIRE_AND_FORGET:
            for socket, message in (
                (socket_a, "CA"),
                (socket_b, "TX"),
                (socket_a, "NY"),
            ):
                await socket.handle_chat_message(message)
                offered.append("alternate_content" in recorder.content_frames()[-1])
        assert offered == [True, True, False]

    async def test_the_side_by_side_limit_comes_from_settings(self):
        user, chat = await _make_chat(
            "sidebyside2", "9999930132", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        best_model = RecordingChatModel(
            always_reply=FRESH_REPLY, model_quality=110, name="winner-model"
        )
        second_model = RecordingChatModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=100, name="second-model"
        )
        with override_settings(FHI_CHAT_SIDE_BY_SIDES_PER_CHAT=0), _patched_router(
            [best_model, second_model]
        ), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")
        assert "alternate_content" not in recorder.content_frames()[-1]

    async def test_turn_row_names_the_winner_runner_up_and_alternate(self):
        user, chat = await _make_chat(
            "turnrow1", "9999930101", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        best_model = RecordingChatModel(
            always_reply=FRESH_REPLY, model_quality=110, name="winner-model"
        )
        second_model = RecordingChatModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=100, name="second-model"
        )

        with _patched_router([best_model, second_model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        assert row.outcome == "ok"
        assert row.use_external is True
        assert row.backends == ["winner-model", "second-model"]
        assert (row.winner_model, row.winner_pass) == ("winner-model", "primary")
        assert row.runner_up_model == "second-model"
        assert row.winner_score > row.runner_up_score > 0
        assert row.closely_tied is True
        assert row.alternate_offered is True
        assert row.alternate_model == "second-model"
        assert row.alternate_cross_model is True
        assert row.preferred == ""
        assert [c["status"] for c in row.calls] == ["scored", "scored"]
        assert row.fanout_ms is not None and row.turn_ms is not None
        # The client gets the row's id with the pair, to echo with its pick.
        frame = recorder.content_frames()[-1]
        assert frame["alternate_content"] == SECOND_OPINION_REPLY
        assert frame["turn_id"] == str(row.id)

    async def test_turn_row_holds_no_text(self):
        user, chat = await _make_chat(
            "turnrow2", "9999930102", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
            state_hint="Nebraska",
        )
        best_model = RecordingChatModel(
            always_reply=FRESH_REPLY, model_quality=110, name="winner-model"
        )
        second_model = RecordingChatModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=100, name="second-model"
        )
        message = "My plan is Zanzibar Mutual and my doctor is Dr. Quillfeather"

        with _patched_router([best_model, second_model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message(message)

        (row,) = await _turn_rows(chat)
        blob = _row_blob(row)
        for text in (
            "Zanzibar",
            "Quillfeather",
            "Nebraska",
            "mock context summary",
            FRESH_REPLY[:40],
            SECOND_OPINION_REPLY[:40],
            LOOPED_REPLY[:40],
        ):
            assert text not in blob, f"{text!r} stored on the turn row"

    async def test_no_turn_id_without_an_alternate(self):
        user, chat = await _make_chat(
            "turnrow3", "9999930103", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY, name="only-model")

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        frame = recorder.content_frames()[-1]
        assert "turn_id" not in frame
        (row,) = await _turn_rows(chat)
        assert row.alternate_offered is False
        assert row.alternate_model == ""

    async def test_repeat_error_and_late_calls_are_recorded(self):
        user, chat = await _make_chat(
            "turnrow4", "9999930104", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        models = [
            RecordingChatModel(
                always_reply=LOOPED_REPLY, model_quality=110, name="looping-backend"
            ),
            _RaisingModel(name="raising-backend"),
            _StalledModel(name="stalled-backend"),
            RecordingChatModel(
                always_reply=FRESH_REPLY, model_quality=100, name="fresh-backend"
            ),
        ]

        with _patched_router(models), _PATCH_FIRE_AND_FORGET, _PATCH_SHORT_RACE:
            await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        statuses = {c["model"]: c["status"] for c in row.calls}
        assert statuses == {
            "looping-backend": "repeat",
            "raising-backend": "error",
            "stalled-backend": "late",
            "fresh-backend": "scored",
        }
        by_model = {c["model"]: c for c in row.calls}
        assert by_model["raising-backend"]["error"] == "RuntimeError"
        assert by_model["looping-backend"]["score"] is None
        assert by_model["stalled-backend"]["ms"] is None
        assert row.winner_model == "fresh-backend"
        assert row.rejected_repeats == 1
        assert row.retry_ran is False
        assert "Blue Shield" not in _row_blob(row)

    async def test_a_turn_over_budget_is_recorded_as_a_timeout(self):
        user, chat = await _make_chat(
            "turnrow5", "9999930105", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)

        with (
            patch.dict(os.environ, {"FHI_CHAT_TURN_BUDGET": "0.3"}),
            _patched_router([_StalledModel(name="stalled-backend")]),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        assert row.outcome == "timeout"
        assert row.winner_model == ""
        assert [(c["model"], c["status"]) for c in row.calls] == [
            ("stalled-backend", "late")
        ]

    async def test_a_turn_with_no_usable_answer_is_recorded_as_failed(self):
        user, chat = await _make_chat(
            "turnrow6", "9999930106", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply="", name="empty-backend")

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        assert row.outcome == "failed"
        assert row.retry_ran is True
        assert row.retry_used is False
        assert row.winner_model == ""
        passes = [(c["pass"], c["status"]) for c in row.calls]
        assert ("retry", "empty") in passes

    async def test_the_alternate_comes_from_another_model_when_one_ties(self):
        """The runner-up is the winner's own model, but a different model's
        answer is closely tied too: the pair shown is model versus model."""
        user, chat = await _make_chat(
            "turnrow7", "9999930107", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        models = [
            RecordingChatModel(
                always_reply=FRESH_REPLY, model_quality=110, name="model-a"
            ),
            RecordingChatModel(
                always_reply=THIRD_REPLY, model_quality=108, name="model-a"
            ),
            RecordingChatModel(
                always_reply=SECOND_OPINION_REPLY, model_quality=100, name="model-b"
            ),
        ]

        with _patched_router(models), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        frame = recorder.content_frames()[-1]
        assert frame["content"] == FRESH_REPLY
        assert frame["alternate_content"] == SECOND_OPINION_REPLY
        (row,) = await _turn_rows(chat)
        assert row.runner_up_model == "model-a"
        assert row.alternate_model == "model-b"
        assert row.alternate_cross_model is True

    async def test_the_same_model_runner_up_is_used_when_no_other_model_ties(self):
        user, chat = await _make_chat(
            "turnrow8", "9999930108", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        models = [
            RecordingChatModel(
                always_reply=FRESH_REPLY, model_quality=110, name="model-a"
            ),
            RecordingChatModel(
                always_reply=THIRD_REPLY, model_quality=108, name="model-a"
            ),
            RecordingChatModel(
                always_reply=SECOND_OPINION_REPLY, model_quality=60, name="model-b"
            ),
        ]

        with _patched_router(models), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("CA")

        frame = recorder.content_frames()[-1]
        assert frame["alternate_content"] == THIRD_REPLY
        (row,) = await _turn_rows(chat)
        assert row.alternate_model == "model-a"
        assert row.alternate_cross_model is False

    async def test_early_replies_that_skip_the_models_leave_no_row(self):
        user, chat = await _make_chat(
            "turnrow9", "9999930109", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY)

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            await interface.handle_chat_message("Please delete my data")

        assert await _turn_rows(chat) == []
        assert model.calls == []

    async def test_an_exception_while_saving_the_reply_records_a_failed_turn(self):
        """The models answered, then saving the reply raised: the turn is
        counted failed and its row says so."""
        user, chat = await _make_chat(
            "turnrow10", "9999930110", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY, name="answering-backend")
        failed_before = _metric("fhi_chat_turns_total", {"outcome": "failed"})
        ok_before = _metric("fhi_chat_turns_total", {"outcome": "ok"})

        with (
            _patched_router([model]),
            _PATCH_FIRE_AND_FORGET,
            _patch_reply_save_to_fail(),
        ):
            with self.assertRaises(OperationalError):
                await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        assert row.outcome == "failed"
        assert row.winner_model == "answering-backend"
        assert [c["status"] for c in row.calls] == ["scored"]
        assert _metric("fhi_chat_turns_total", {"outcome": "failed"}) == (
            failed_before + 1
        )
        assert _metric("fhi_chat_turns_total", {"outcome": "ok"}) == ok_before

    async def test_an_exception_before_the_models_are_asked_leaves_no_row(self):
        user, chat = await _make_chat(
            "turnrow11", "9999930111", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY)
        failed_before = _metric("fhi_chat_turns_total", {"outcome": "failed"})

        with (
            _patched_router([model]),
            _PATCH_FIRE_AND_FORGET,
            patch(
                "fighthealthinsurance.chat_interface.prepare_history_for_llm",
                side_effect=RuntimeError("history broke"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await interface.handle_chat_message("CA")

        assert model.calls == []
        assert await _turn_rows(chat) == []
        assert _metric("fhi_chat_turns_total", {"outcome": "failed"}) == failed_before

    async def test_a_reply_frame_that_fails_to_send_keeps_its_ok_row(self):
        """The metric counts the turn "ok" before the frame goes out, so the
        row keeps "ok" when the send itself raises."""
        user, chat = await _make_chat(
            "turnrow12", "9999930112", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()

        async def send(frame):
            await recorder(frame)
            if "content" in frame:
                raise ConnectionError("socket closed")

        interface = ChatInterface(send_json_message_func=send, chat=chat, user=user)
        model = RecordingChatModel(always_reply=FRESH_REPLY, name="answering-backend")
        failed_before = _metric("fhi_chat_turns_total", {"outcome": "failed"})

        with _patched_router([model]), _PATCH_FIRE_AND_FORGET:
            with self.assertRaises(ConnectionError):
                await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        assert (row.outcome, row.winner_model) == ("ok", "answering-backend")
        assert _metric("fhi_chat_turns_total", {"outcome": "failed"}) == failed_before

    async def test_a_tool_follow_up_from_another_model_is_credited_with_the_reply(
        self,
    ):
        """model-a's first-pass reply asks for a tool; the tool's follow-up
        pass is won by model-b, whose reply replaces it. The row credits
        model-b, the model the person heard from, and keeps model-a as the
        first pass's pick."""
        user, chat = await _make_chat(
            "turnrow13", "9999930113", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        tool_asker = _ToolAskingModel(model_quality=110, name="model-a")
        follow_up_writer = _FollowUpModel(model_quality=100, name="model-b")

        with (
            _patched_router([tool_asker, follow_up_writer]),
            _PATCH_FIRE_AND_FORGET,
            patch(
                "fighthealthinsurance.medicaid_api.get_medicaid_info",
                return_value="Medi-Cal: apply online or at the county office.",
            ),
        ):
            await interface.handle_chat_message("CA")

        assert recorder.content_frames()[-1]["content"] == FRESH_REPLY
        (row,) = await _turn_rows(chat)
        assert row.outcome == "ok"
        assert (row.winner_model, row.winner_pass) == ("model-b", "tool")
        assert row.winner_external is True
        assert row.retry_used is False
        assert row.first_pass_model == "model-a"
        assert row.first_pass_score is not None
        assert row.tool_rewrote is True
        assert row.tool_passes == 1
        tool_calls = {c["model"]: c["status"] for c in row.calls if c["pass"] == "tool"}
        assert tool_calls == {"model-a": "empty", "model-b": "scored"}

    async def test_a_call_that_answered_before_the_budget_ran_out_is_not_late(self):
        """The race waits for every call before scoring, so when the turn
        budget runs out first, a call that had answered is unscored and
        keeps its time; only the call still running is late."""
        user, chat = await _make_chat(
            "turnrow14", "9999930114", chat_history=_seed_history()
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(send_json_message_func=recorder, chat=chat, user=user)
        models = [
            RecordingChatModel(always_reply=FRESH_REPLY, name="quick-backend"),
            _StalledModel(name="stalled-backend"),
        ]

        with (
            patch.dict(os.environ, {"FHI_CHAT_TURN_BUDGET": "0.5"}),
            _patched_router(models),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message("CA")

        (row,) = await _turn_rows(chat)
        assert row.outcome == "timeout"
        by_model = {c["model"]: c for c in row.calls}
        assert by_model["quick-backend"]["status"] == "unscored"
        assert isinstance(by_model["quick-backend"]["ms"], int)
        assert by_model["quick-backend"]["score"] is None
        assert by_model["stalled-backend"]["status"] == "late"
        assert by_model["stalled-backend"]["ms"] is None


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
        assert result["alternate_candidate"] is True
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

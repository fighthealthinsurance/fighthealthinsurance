"""End-to-end chat turns with the live Jev check on our reply ("cascade").

When the check is on for a turn, the outside models' calls wait while ours
answer and Jev checks our first usable reply: a pass means they are never
sent, and anything else (a fail, an error, a timeout, an answer we cannot
read) starts them at once. With the check off, or when the person has not
allowed outside models, a turn runs exactly as before and nothing reaches
TypeSafe. Each ChatTurn row records what the check did, with numbers and
labels only.
"""

import asyncio
import threading
import typing
from unittest.mock import AsyncMock, patch

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.contrib.auth import get_user_model
from django.test import override_settings
from loguru import logger
from rest_framework.test import APITestCase, APITransactionTestCase

from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.chat.message_preprocessor import DIRECT_CHAT_HARD_LIMIT_CHARS
from fighthealthinsurance.ml import chat_gate, typesafe
from fighthealthinsurance.ml.chat_policy import ChatPolicy
from fighthealthinsurance.models import (
    ChatTurn,
    ExternalServiceHealth,
    OngoingChat,
    ProfessionalUser,
)
from tests.chat_fixtures import FRESH_REPLY, SECOND_OPINION_REPLY, RecordingChatModel

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

ENABLED = dict(TYPESAFE_API_KEY="test-key", FHI_CHAT_JEV_GATE_ENABLED=True)
MESSAGE = "What is an appeal? I am Robin Quill, reach me at robin.q@example.com."


class _OutsideModel(RecordingChatModel):
    external = True


class _OursModel(RecordingChatModel):
    external = False


class _SlowOursModel(_OursModel):
    def __init__(self, *args, delay=1.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay

    async def generate_chat_response(self, *args, **kwargs):
        await asyncio.sleep(self.delay)
        return await super().generate_chat_response(*args, **kwargs)


class _BrokenOutsideModel(_OutsideModel):
    async def generate_chat_response(self, *args, **kwargs):
        await super().generate_chat_response(*args, **kwargs)
        raise RuntimeError("outside backend down")


class _Frames:
    def __init__(self):
        self.frames = []

    async def __call__(self, frame):
        self.frames.append(frame)

    def last_content(self):
        return [f for f in self.frames if "content" in f][-1]["content"]

    def last_reply_frame(self):
        return [f for f in self.frames if "content" in f][-1]


class _Jev:
    """Stands in for the TypeSafe request (chat_gate._post): records each
    state it was sent and answers as told."""

    def __init__(self, payload=None, error=None, delay=0.0):
        self.payload = payload
        self.error = error
        self.delay = delay
        self.states = []

    async def __call__(self, state, timeout):
        self.states.append(state)
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.payload


def _answers(answers=0.9, verdict=0.05, asks_again=0.05):
    return {
        "model": "jev-1.13.0",
        "answers": {
            chat_gate.ANSWERS_QUESTION: {"type": "noul", "noul": answers},
            chat_gate.STATES_VERDICT: {"type": "noul", "noul": verdict},
            chat_gate.ASKS_AGAIN: {"type": "noul", "noul": asks_again},
        },
    }


async def _make_chat(username, npi):
    user = await User.objects.acreate_user(
        username=username,
        password="testpass",
        email=f"{username}@example.com",
        first_name="Robin",
        last_name="Quill",
    )
    professional = await ProfessionalUser.objects.acreate(
        user=user, active=True, npi_number=npi
    )
    chat = await OngoingChat.objects.acreate(
        professional_user=professional, chat_history=[], summary_for_next_call=[]
    )
    return user, chat


def _router_returning(models):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
        return_value=(models, []),
    )


def _ours_selectable(selectable=True):
    return patch(
        "fighthealthinsurance.ml.ml_router.MLRouter._healthy_general_internal",
        return_value=["fhi-local"] if selectable else [],
    )


def _jev(jev):
    return patch.object(chat_gate, "_post", new=jev)


def _policy(policy):
    return patch(
        "fighthealthinsurance.chat_interface.aget_chat_policy",
        new=AsyncMock(return_value=policy),
    )


_PATCH_FIRE_AND_FORGET = patch(
    "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
    new_callable=AsyncMock,
)


def _pair():
    ours = _OursModel(always_reply=FRESH_REPLY, model_quality=110, name="fhi-local")
    outside = _OutsideModel(
        always_reply=SECOND_OPINION_REPLY, model_quality=60, name="claude"
    )
    return ours, outside


async def _only_row(chat):
    return await ChatTurn.objects.filter(chat=chat).aget()


async def _health():
    return await ExternalServiceHealth.objects.filter(
        service=chat_gate.SERVICE
    ).afirst()


def _statuses(row):
    return sorted({(c["model"], c["status"]) for c in row.calls})


def _row_values(row):
    return [getattr(row, f.attname) for f in ChatTurn._meta.concrete_fields]


class _Logs:
    """Loguru records from the check's own modules during a turn."""

    MODULES = (
        "fighthealthinsurance.chat.reply_gate",
        "fighthealthinsurance.ml.chat_gate",
        "fighthealthinsurance.utils",
    )

    def __enter__(self):
        self.messages = []
        self._sink = logger.add(
            lambda m: (
                self.messages.append(m.record["message"])
                if m.record["name"] in self.MODULES
                else None
            ),
            level="DEBUG",
        )
        return self

    def __exit__(self, *exc):
        logger.remove(self._sink)


class ChatReplyCheckOffTest(APITestCase):
    """Every case where the check must not run: no request to TypeSafe and
    the turn routes exactly as without the check."""

    async def _turn_without_check(
        self, username, npi, *, use_external=True, models=None
    ):
        user, chat = await _make_chat(username, npi)
        frames = _Frames()
        interface = ChatInterface(
            send_json_message_func=frames,
            chat=chat,
            user=user,
            use_external_models=use_external,
        )
        ours, outside = _pair()
        jev = _Jev(payload=_answers())
        with (
            _ours_selectable(),
            _router_returning(models(ours, outside) if models else [ours, outside]),
            _jev(jev),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message(MESSAGE)
        self.assertEqual(jev.states, [])
        row = await _only_row(chat)
        self.assertFalse(row.gate_used)
        self.assertEqual(row.gate_outcome, "")
        self.assertEqual(
            (row.gate_answers, row.gate_scorer, row.gate_ms, row.gate_model),
            (None, "", None, ""),
        )
        return row, outside

    async def test_off_by_default(self):
        row, outside = await self._turn_without_check("gateoff1", "9999931001")
        self.assertTrue(outside.calls)
        self.assertEqual(row.external_start, "immediate")

    async def test_off_without_the_key(self):
        with override_settings(TYPESAFE_API_KEY=None, FHI_CHAT_JEV_GATE_ENABLED=True):
            row, outside = await self._turn_without_check("gateoff2", "9999931002")
        self.assertTrue(outside.calls)
        self.assertEqual(row.external_start, "immediate")

    async def test_off_without_consent_to_outside_models(self):
        with override_settings(**ENABLED):
            row, outside = await self._turn_without_check(
                "gateoff3",
                "9999931003",
                use_external=False,
                models=lambda ours, outside: [ours],
            )
        self.assertEqual(outside.calls, [])
        self.assertEqual(row.external_start, "")

    async def test_off_when_the_turn_has_no_outside_model(self):
        with override_settings(**ENABLED):
            row, _outside = await self._turn_without_check(
                "gateoff4", "9999931004", models=lambda ours, outside: [ours]
            )
        self.assertEqual(row.external_start, "")

    async def test_off_when_none_of_ours_is_selectable(self):
        user, chat = await _make_chat("gateoff5", "9999931005")
        interface = ChatInterface(
            send_json_message_func=_Frames(), chat=chat, user=user
        )
        ours, outside = _pair()
        jev = _Jev(payload=_answers())
        with (
            override_settings(**ENABLED),
            _ours_selectable(False),
            _router_returning([ours, outside]),
            _jev(jev),
            _PATCH_FIRE_AND_FORGET,
        ):
            await interface.handle_chat_message(MESSAGE)
        self.assertEqual(jev.states, [])
        self.assertTrue(outside.calls)
        row = await _only_row(chat)
        self.assertFalse(row.gate_used)
        self.assertEqual(row.external_start, "immediate")

    async def test_off_for_a_stored_long_paste(self):
        user, chat = await _make_chat("gateoff6", "9999931006")
        interface = ChatInterface(
            send_json_message_func=_Frames(), chat=chat, user=user
        )
        ours, outside = _pair()
        jev = _Jev(payload=_answers())
        big = "My plan denied the claim as not medically necessary. " * (
            DIRECT_CHAT_HARD_LIMIT_CHARS // 40
        )
        with (
            override_settings(**ENABLED),
            _ours_selectable(),
            _router_returning([ours, outside]),
            _jev(jev),
            _PATCH_FIRE_AND_FORGET,
            patch(
                "fighthealthinsurance.chat_interface.process_uploaded_document",
                new_callable=AsyncMock,
            ),
        ):
            await interface.handle_chat_message(big)
        self.assertEqual(jev.states, [])
        self.assertTrue(outside.calls)
        row = await _only_row(chat)
        self.assertFalse(row.gate_used)


class ChatReplyCheckTest(APITransactionTestCase):
    """Transactional: the check reads the chat's identifiers, and notes its
    health, on threads of their own (chat/isolated_db.py), which only see
    committed rows."""

    async def _turn(
        self,
        username,
        npi,
        jev,
        *,
        ours=None,
        outside=None,
        settings=None,
        policy=None,
    ):
        user, chat = await _make_chat(username, npi)
        frames = _Frames()
        interface = ChatInterface(send_json_message_func=frames, chat=chat, user=user)
        default_ours, default_outside = _pair()
        ours = ours or default_ours
        outside = outside or default_outside
        loop = asyncio.get_running_loop()
        started = loop.time()
        patches = [_policy(policy)] if policy is not None else []
        with (
            override_settings(**{**ENABLED, **(settings or {})}),
            _ours_selectable(),
            _router_returning([ours, outside]),
            _jev(jev),
            _PATCH_FIRE_AND_FORGET,
            _Logs() as logs,
        ):
            for p in patches:
                p.start()
            try:
                await interface.handle_chat_message(MESSAGE)
            finally:
                for p in patches:
                    p.stop()
        elapsed = loop.time() - started
        row = await _only_row(chat)
        return row, outside, frames, elapsed, logs

    def _assert_no_text(self, row, logs, *replies):
        values = _row_values(row) + [str(c) for c in row.calls]
        blob = " ".join(str(v) for v in values) + " ".join(logs.messages)
        for text in (MESSAGE, *replies):
            for piece in (text, text[:24], text[-24:]):
                self.assertNotIn(piece, blob)

    async def test_a_pass_means_the_outside_models_are_never_sent(self):
        jev = _Jev(payload=_answers())
        row, outside, frames, elapsed, logs = await self._turn(
            "gate1", "9999931101", jev
        )
        self.assertEqual(outside.calls, [])
        self.assertEqual(frames.last_content(), FRESH_REPLY)
        # It did not sit out the check's 8 second hold.
        self.assertLess(elapsed, 4.0)
        self.assertEqual(len(jev.states), 1)
        self.assertTrue(row.gate_used)
        self.assertEqual(row.gate_outcome, "pass")
        self.assertEqual(
            (row.gate_answers, row.gate_verdict, row.gate_asks_again),
            (0.9, 0.05, 0.05),
        )
        self.assertEqual(row.gate_scorer, "typesafe/jev-1.13.0/chat-gate-rubric-1")
        self.assertEqual(row.gate_model, "fhi-local")
        self.assertIsInstance(row.gate_ms, int)
        self.assertEqual(row.external_start, "skipped")
        self.assertEqual(row.external_delay_seconds, 8.0)
        self.assertEqual(
            _statuses(row), [("claude", "skipped"), ("fhi-local", "scored")]
        )
        self.assertEqual(row.winner_model, "fhi-local")
        # A pass demotes nothing.
        self.assertFalse(row.gate_demoted)
        self.assertFalse(row.gate_demoted_delivered)
        health = await _health()
        self.assertIsNotNone(health.last_success_at)
        self.assertIsNone(health.last_failure_at)
        self._assert_no_text(row, logs, FRESH_REPLY, SECOND_OPINION_REPLY)

    async def test_the_state_sent_is_the_message_and_our_reply_redacted(self):
        jev = _Jev(payload=_answers())
        await self._turn("gate2", "9999931102", jev)
        (state,) = jev.states
        self.assertTrue(state.startswith(chat_gate.MESSAGE_HEADER))
        self.assertIn("What is an appeal?", state)
        self.assertIn(FRESH_REPLY, state)
        self.assertNotIn(SECOND_OPINION_REPLY, state)
        for identifier in ("Robin", "Quill", "robin.q@example.com"):
            self.assertNotIn(identifier, state)

    async def test_a_fail_starts_the_outside_models_and_one_of_them_wins(self):
        jev = _Jev(payload=_answers(answers=0.2))
        row, outside, frames, elapsed, logs = await self._turn(
            "gate3", "9999931103", jev
        )
        self.assertTrue(outside.calls)
        self.assertLess(elapsed, 4.0)
        self.assertEqual(row.gate_outcome, "fail")
        self.assertEqual(row.gate_answers, 0.2)
        self.assertEqual(row.external_start, "after_check")
        self.assertIn(("claude", "scored"), _statuses(row))
        # Our failed reply ranks below the outside answer, whatever its
        # higher base score, and is not offered beside it either.
        self.assertEqual(row.winner_model, "claude")
        self.assertTrue(row.winner_external)
        self.assertEqual(frames.last_content(), SECOND_OPINION_REPLY)
        self.assertNotIn("alternate_content", frames.last_reply_frame())
        self.assertEqual(row.alternate_model, "")
        self.assertTrue(row.gate_demoted)
        self.assertFalse(row.gate_demoted_delivered)
        self._assert_no_text(row, logs, FRESH_REPLY, SECOND_OPINION_REPLY)

    async def test_a_fail_with_no_outside_answer_still_delivers_ours(self):
        """Never hold the reply hostage: with nothing else usable, the
        demoted reply is still the one delivered."""
        jev = _Jev(payload=_answers(answers=0.2))
        broken = _BrokenOutsideModel(
            always_reply=SECOND_OPINION_REPLY, model_quality=60, name="claude"
        )
        row, outside, frames, _elapsed, logs = await self._turn(
            "gate13", "9999931113", jev, outside=broken
        )
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "fail")
        self.assertEqual(row.winner_model, "fhi-local")
        self.assertEqual(frames.last_content(), FRESH_REPLY)
        self.assertTrue(row.gate_demoted)
        self.assertTrue(row.gate_demoted_delivered)
        # Ranked below the outside calls' base score, though none arrived.
        (ours_score,) = [
            c["score"] for c in row.calls if c["model"] == "fhi-local" and c["score"]
        ]
        self.assertLess(row.winner_score, ours_score)
        self._assert_no_text(row, logs, FRESH_REPLY, SECOND_OPINION_REPLY)

    async def test_with_demotion_off_a_fail_keeps_the_usual_scoring(self):
        jev = _Jev(payload=_answers(answers=0.2))
        row, outside, frames, _elapsed, _logs = await self._turn(
            "gate14",
            "9999931114",
            jev,
            settings={"FHI_CHAT_JEV_GATE_DEMOTE_FAILED": False},
        )
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "fail")
        # Ours outscores the outside model on its base score.
        self.assertEqual(row.winner_model, "fhi-local")
        self.assertEqual(frames.last_content(), FRESH_REPLY)
        self.assertFalse(row.gate_demoted)
        self.assertFalse(row.gate_demoted_delivered)

    async def test_a_verdict_fails_the_check(self):
        jev = _Jev(payload=_answers(verdict=0.9))
        row, outside, _frames, _elapsed, _logs = await self._turn(
            "gate4", "9999931104", jev
        )
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "fail")

    async def test_a_timeout_starts_the_outside_models(self):
        jev = _Jev(payload=_answers(), delay=5.0)
        row, outside, _frames, elapsed, logs = await self._turn(
            "gate5",
            "9999931105",
            jev,
            settings={"FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS": 1.0},
        )
        self.assertTrue(outside.calls)
        self.assertLess(elapsed, 4.0)
        self.assertEqual(row.gate_outcome, "timeout")
        self.assertIsNone(row.gate_answers)
        self.assertEqual(row.gate_scorer, "")
        self.assertEqual(row.external_start, "after_check")
        # A timeout says nothing about our reply: no demotion.
        self.assertEqual(row.winner_model, "fhi-local")
        self.assertFalse(row.gate_demoted)
        health = await _health()
        self.assertEqual(health.last_failure, "timeout")
        self._assert_no_text(row, logs, FRESH_REPLY, SECOND_OPINION_REPLY)

    async def test_an_http_error_starts_the_outside_models(self):
        jev = _Jev(error=typesafe.TypeSafeError("HTTP 500", status=500))
        row, outside, _frames, _elapsed, logs = await self._turn(
            "gate6", "9999931106", jev
        )
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "error")
        self.assertEqual(row.external_start, "after_check")
        # Nor does an error.
        self.assertEqual(row.winner_model, "fhi-local")
        self.assertFalse(row.gate_demoted)
        health = await _health()
        self.assertEqual(health.last_failure, "HTTP 500")

    async def test_an_answer_we_cannot_read_starts_the_outside_models(self):
        jev = _Jev(payload={"model": "jev-1.13.0", "answers": {"x": 1}})
        row, outside, _frames, _elapsed, _logs = await self._turn(
            "gate7", "9999931107", jev
        )
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "error")
        self.assertIsNone(row.gate_answers)

    async def test_the_hold_runs_out_when_ours_are_slow(self):
        jev = _Jev(payload=_answers())
        ours = _SlowOursModel(
            always_reply=FRESH_REPLY, model_quality=110, name="fhi-local", delay=1.0
        )
        row, outside, frames, _elapsed, _logs = await self._turn(
            "gate8",
            "9999931108",
            jev,
            ours=ours,
            settings={"FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS": 0.5},
        )
        # Nothing of ours was usable within the hold: the outside models
        # started when it ran out, and nothing was judged.
        self.assertTrue(outside.calls)
        self.assertEqual(jev.states, [])
        self.assertTrue(row.gate_used)
        self.assertEqual(row.gate_outcome, "skipped")
        self.assertEqual(row.external_start, "after_delay")
        self.assertEqual(row.external_delay_seconds, 0.5)
        self.assertEqual(frames.last_content(), FRESH_REPLY)

    async def test_the_check_holds_without_the_routing_policy(self):
        """FHI_CHAT_POLICY_APPLY is off under test, so the policy's delay is
        0; the check still holds the outside models on its own."""
        jev = _Jev(payload=_answers())
        row, outside, _frames, _elapsed, _logs = await self._turn(
            "gate9", "9999931109", jev
        )
        self.assertEqual(outside.calls, [])
        self.assertEqual(row.external_start, "skipped")

    async def test_a_longer_policy_delay_is_the_hold(self):
        jev = _Jev(payload=_answers())
        row, outside, _frames, _elapsed, _logs = await self._turn(
            "gate10",
            "9999931110",
            jev,
            policy=ChatPolicy(external_delay_seconds=12.0, reason="ok"),
        )
        self.assertEqual(outside.calls, [])
        self.assertEqual(row.gate_outcome, "pass")
        self.assertEqual(row.external_delay_seconds, 12.0)

    async def test_a_reply_carrying_a_tool_call_is_not_sent_to_jev(self):
        jev = _Jev(payload=_answers())
        tool_reply = (
            "Let me look that up for you. "
            '**medicaid_info {"state": "California", "topic": "", "limit": 5}**'
        )
        ours = _OursModel(always_reply=tool_reply, model_quality=110, name="fhi-local")
        row, outside, _frames, _elapsed, _logs = await self._turn(
            "gate11", "9999931111", jev, ours=ours
        )
        self.assertEqual(jev.states, [])
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "skipped")
        self.assertEqual(row.external_start, "after_check")
        self.assertEqual(row.gate_model, "fhi-local")

    async def test_a_stuck_identifier_lookup_does_not_hold_up_the_turn(self):
        """The lookup runs off the chat's database executor: when it is
        stuck, the check gives up after its timeout, the outside models
        start, and the rest of the turn (its row included) runs on the
        chat's executor without waiting for it."""
        entered, release = threading.Event(), threading.Event()

        def stuck(chat_id):
            entered.set()
            release.wait(10)
            return []

        jev = _Jev(payload=_answers())
        try:
            # The socket's own executor, as PerConnectionThreadSensitiveMixin
            # sets up for a chat.
            async with ThreadSensitiveContext():
                with patch(
                    "fighthealthinsurance.chat.reply_gate.chat_redactions", new=stuck
                ):
                    row, outside, frames, elapsed, _logs = await self._turn(
                        "gate12",
                        "9999931112",
                        jev,
                        settings={"FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS": 0.3},
                    )
                    still_stuck = entered.is_set() and not release.is_set()
        finally:
            release.set()
        self.assertTrue(still_stuck)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(jev.states, [])
        self.assertTrue(outside.calls)
        self.assertEqual(row.gate_outcome, "timeout")
        self.assertEqual(row.external_start, "after_check")
        self.assertEqual(frames.last_content(), FRESH_REPLY)

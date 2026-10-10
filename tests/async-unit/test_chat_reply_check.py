"""The live Jev check on our own chat reply (ml/chat_gate.py and
chat/reply_gate.py).

The check decides whether the paid outside models are asked, so its failure
mode matters most: any doubt (an error, a timeout, an answer we cannot read)
must mean "not judged", which starts the outside models, never a pass. It
sends chat text to TypeSafe, so it must be off unless the switch, the key
and the person's consent all allow it, and it must never store or log the
text.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.test import override_settings

from fighthealthinsurance.chat import isolated_db, redaction, reply_gate
from fighthealthinsurance.chat.retry_handler import should_retry_response
from fighthealthinsurance.chat.safety_filters import (
    DELETE_DATA_SENTINEL,
    detect_false_promises,
)
from fighthealthinsurance.chat.turn_record import TurnRecord
from fighthealthinsurance.chat_interface import _deliverable
from fighthealthinsurance.ml import chat_gate, typesafe
from fighthealthinsurance.models import ChatTurn, ExternalServiceHealth, OngoingChat

ENABLED = dict(TYPESAFE_API_KEY="test-key", FHI_CHAT_JEV_GATE_ENABLED=True)

MESSAGE = "My plan denied my MRI. Email me at pat.doe@example.com or 415-555-0100."
REPLY = (
    "Here is how to appeal an MRI denial: ask for the denial letter, then "
    "write to the plan. Want me to draft the appeal?"
)
TOOL_REPLY = '**medicaid_info {"state": "California", "topic": "", "limit": 5}**'
# Replies our own checks reject (chat/retry_handler.should_retry_response).
PROMISE_REPLY = (
    "Good news: I guarantee your appeal will be approved. Send the plan the "
    "denial letter and a note from your doctor."
)
SHORT_REPLY = "Ok."


def _payload(
    answers=0.9,
    verdict=0.05,
    asks_again=0.05,
    promises=0.05,
    model="jev-1.13.0",
    crucial=0.1,
):
    return {
        "model": model,
        "answers": {
            chat_gate.ANSWERS_QUESTION: {"type": "noul", "noul": answers},
            chat_gate.STATES_VERDICT: {"type": "noul", "noul": verdict},
            chat_gate.ASKS_AGAIN: {"type": "noul", "noul": asks_again},
            chat_gate.PROMISES_OUTCOME: {"type": "noul", "noul": promises},
            chat_gate.CRUCIAL_MOMENT: {"type": "noul", "noul": crucial},
        },
        "usage": {"input_tokens": 321, "output_tokens": 30},
    }


def _no_text(values, *texts):
    """None of the texts, nor a telling piece of one, appears in values."""
    blob = " ".join(str(v) for v in values)
    for text in texts:
        for piece in (text, text[:24], text[-24:]):
            assert piece not in blob


# --- Switches and knobs -----------------------------------------------------


class TestEnabled:
    def test_off_by_default_under_test(self):
        assert chat_gate.enabled() is False

    def test_off_without_a_key(self):
        with override_settings(TYPESAFE_API_KEY=None, FHI_CHAT_JEV_GATE_ENABLED=True):
            assert chat_gate.enabled() is False

    def test_off_without_the_switch(self):
        with override_settings(TYPESAFE_API_KEY="k", FHI_CHAT_JEV_GATE_ENABLED=False):
            assert chat_gate.enabled() is False

    def test_on_with_both(self):
        with override_settings(**ENABLED):
            assert chat_gate.enabled() is True


class TestKnobs:
    def test_defaults(self):
        assert chat_gate.max_wait_seconds() == 8.0
        assert chat_gate.timeout_seconds() == 1.5
        assert chat_gate.min_answers() == 0.7
        assert chat_gate.max_problem() == 0.3

    @pytest.mark.parametrize("value", [0.0, 31.0, float("nan"), "soon", True, None])
    def test_a_max_wait_outside_its_bounds_falls_back(self, value):
        with override_settings(FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS=value):
            assert chat_gate.max_wait_seconds() == 8.0

    @pytest.mark.parametrize("value", [0.0, 11.0, float("inf"), "fast"])
    def test_a_timeout_outside_its_bounds_falls_back(self, value):
        with override_settings(FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS=value):
            assert chat_gate.timeout_seconds() == 1.5

    @pytest.mark.parametrize("value", [-0.1, 1.5, float("nan")])
    def test_a_threshold_outside_zero_to_one_falls_back(self, value):
        with override_settings(
            FHI_CHAT_JEV_GATE_MIN_ANSWERS=value, FHI_CHAT_JEV_GATE_MAX_PROBLEM=value
        ):
            assert chat_gate.min_answers() == 0.7
            assert chat_gate.max_problem() == 0.3

    def test_demotion_is_on_by_default(self):
        assert chat_gate.demote_failed() is True

    def test_demotion_can_be_switched_off(self):
        with override_settings(FHI_CHAT_JEV_GATE_DEMOTE_FAILED=False):
            assert chat_gate.demote_failed() is False

    @pytest.mark.parametrize("value", ["false", 0, None])
    def test_a_demotion_setting_that_is_not_a_boolean_means_the_default(self, value):
        with override_settings(FHI_CHAT_JEV_GATE_DEMOTE_FAILED=value):
            assert chat_gate.demote_failed() is True

    def test_settings_inside_the_bounds_are_used(self):
        with override_settings(
            FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS=3,
            FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS=0.5,
            FHI_CHAT_JEV_GATE_MIN_ANSWERS=0.6,
            FHI_CHAT_JEV_GATE_MAX_PROBLEM=0.2,
        ):
            assert chat_gate.max_wait_seconds() == 3.0
            assert chat_gate.timeout_seconds() == 0.5
            assert chat_gate.min_answers() == 0.6
            assert chat_gate.max_problem() == 0.2


# --- The decision rule ------------------------------------------------------


class TestDecisionRule:
    def test_a_reply_that_answers_with_no_problem_passes(self):
        assert chat_gate.passes(chat_gate.GateScores(0.9, 0.1, 0.1, 0.1))

    def test_the_answers_threshold_is_inclusive(self):
        assert chat_gate.passes(chat_gate.GateScores(0.7, 0.0, 0.0, 0.0))
        assert not chat_gate.passes(chat_gate.GateScores(0.69, 0.0, 0.0, 0.0))

    def test_each_problem_threshold_is_exclusive(self):
        assert not chat_gate.passes(chat_gate.GateScores(1.0, 0.3, 0.0, 0.0))
        assert not chat_gate.passes(chat_gate.GateScores(1.0, 0.0, 0.3, 0.0))
        assert not chat_gate.passes(chat_gate.GateScores(1.0, 0.0, 0.0, 0.3))
        assert chat_gate.passes(chat_gate.GateScores(1.0, 0.29, 0.29, 0.29))

    def test_a_promised_result_fails_a_reply_that_is_otherwise_fine(self):
        assert not chat_gate.passes(chat_gate.GateScores(1.0, 0.0, 0.0, 0.9))
        with override_settings(FHI_CHAT_JEV_GATE_MAX_PROBLEM=0.5):
            assert not chat_gate.passes(chat_gate.GateScores(1.0, 0.0, 0.0, 0.5))
            assert chat_gate.passes(chat_gate.GateScores(1.0, 0.0, 0.0, 0.49))

    def test_the_thresholds_are_settings(self):
        scores = chat_gate.GateScores(0.65, 0.35, 0.0, 0.0)
        assert not chat_gate.passes(scores)
        with override_settings(
            FHI_CHAT_JEV_GATE_MIN_ANSWERS=0.6, FHI_CHAT_JEV_GATE_MAX_PROBLEM=0.4
        ):
            assert chat_gate.passes(scores)


class TestOurOwnChecks:
    """The requirements the retry holds our replies to, applied before Jev
    is asked."""

    @pytest.mark.parametrize(
        "reply", [None, "", "   ", SHORT_REPLY, PROMISE_REPLY, REPLY, TOOL_REPLY]
    )
    def test_the_same_rule_as_the_retry(self, reply):
        assert chat_gate.fails_our_checks(reply) is should_retry_response(reply)

    def test_a_false_promise_a_short_reply_and_a_good_one(self):
        assert detect_false_promises(PROMISE_REPLY)
        assert chat_gate.fails_our_checks(PROMISE_REPLY)
        assert chat_gate.fails_our_checks(SHORT_REPLY)
        assert not chat_gate.fails_our_checks(REPLY)

    def test_no_retry_is_logged_where_none_starts(self, log_capture):
        """The retry's "triggering retry" line comes only from the retry:
        not from the check, nor from weighing an outside answer against a
        demoted reply."""
        with log_capture() as cap:
            assert chat_gate.fails_our_checks(PROMISE_REPLY)
            assert not _deliverable((PROMISE_REPLY, None))
        assert not [r for r in cap.records if "retry" in r["message"]]
        with log_capture() as cap:
            assert should_retry_response(PROMISE_REPLY)
        assert [r for r in cap.records if "triggering retry" in r["message"]]

    def test_the_local_scorer_is_told_apart_from_jevs(self):
        assert chat_gate.from_our_checks(chat_gate.LOCAL_SCORER)
        assert chat_gate.from_our_checks("fhi/local-checks-2")
        assert not chat_gate.from_our_checks(chat_gate.SCORER)
        assert not chat_gate.from_our_checks("")
        assert not chat_gate.from_our_checks(None)
        assert not chat_gate.same_rubric(chat_gate.LOCAL_SCORER)


class TestParseAnswers:
    def test_reads_the_five_answers(self):
        scores = chat_gate.parse_answers(_payload(0.8, 0.2, 0.1, 0.15, crucial=0.6))
        assert scores == chat_gate.GateScores(0.8, 0.2, 0.1, 0.15, 0.6)

    def test_a_payload_without_the_crucial_answer_raises(self):
        payload = _payload()
        del payload["answers"][chat_gate.CRUCIAL_MOMENT]
        with pytest.raises(chat_gate.ChatGateError):
            chat_gate.parse_answers(payload)

    def test_a_payload_without_the_promise_answer_raises(self):
        payload = _payload()
        del payload["answers"][chat_gate.PROMISES_OUTCOME]
        with pytest.raises(chat_gate.ChatGateError):
            chat_gate.parse_answers(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"answers": {}},
            {"answers": None},
            _payload(answers=1.2),
            _payload(verdict=-0.1),
            _payload(asks_again=float("nan")),
            _payload(promises=1.5),
            _payload(promises=None),
            _payload(answers=True),
            _payload(answers="0.9"),
            "not json at all",
            None,
        ],
    )
    def test_anything_unexpected_raises(self, payload):
        with pytest.raises(chat_gate.ChatGateError):
            chat_gate.parse_answers(payload)


class TestState:
    def test_names_both_parts_and_redacts(self):
        redactor = chat_gate.letter_quality.Redactor([("Pat Doe", "PATIENT#p")])
        state = chat_gate.build_state(
            "I am Pat Doe, pat.doe@example.com, 415-555-0100.",
            "Thanks Pat Doe, I will write to 415-555-0100.",
            redactor,
        )
        assert state.startswith(chat_gate.MESSAGE_HEADER)
        assert chat_gate.REPLY_HEADER in state
        for secret in ("Pat Doe", "pat.doe@example.com", "415-555-0100"):
            assert secret not in state
        # The same value gets the same token in both parts.
        assert state.count("[PHONE_1]") == 2

    def test_the_reply_survives_and_the_message_gives_way(self):
        redactor = chat_gate.letter_quality.Redactor()
        state = chat_gate.build_state("M" * 30_000, "R" * 20_000, redactor)
        assert len(state) <= chat_gate.STATE_CHAR_CAP
        assert state.endswith("R" * 20_000)

    def test_five_questions_each_pointing_at_the_named_parts(self):
        assert set(chat_gate.QUESTIONS) == {
            chat_gate.ANSWERS_QUESTION,
            chat_gate.STATES_VERDICT,
            chat_gate.ASKS_AGAIN,
            chat_gate.PROMISES_OUTCOME,
            chat_gate.CRUCIAL_MOMENT,
        }
        for name, question in chat_gate.QUESTIONS.items():
            assert question["type"] == "noul"
            # The crucial question is about the message alone.
            assert ("THE REPLY" in question["instructions"]) is (
                name != chat_gate.CRUCIAL_MOMENT
            )
            assert "THE PERSON'S MESSAGE" in question["instructions"] or name in (
                chat_gate.STATES_VERDICT,
                chat_gate.PROMISES_OUTCOME,
            )

    def test_the_scorer_names_the_answering_model_and_the_rubric(self):
        assert chat_gate.RUBRIC_VERSION == 3
        scorer = chat_gate.scorer_for({"model": "jev-1.14.0"})
        assert scorer == "typesafe/jev-1.14.0/chat-gate-rubric-3"
        assert not chat_gate.same_rubric("typesafe/jev-1.14.0/chat-gate-rubric-2")
        assert chat_gate.same_rubric(scorer)
        assert not chat_gate.same_rubric("typesafe/jev-1.13.0/rubric-1")
        assert chat_gate.scorer_for({}) == chat_gate.SCORER


# --- One request ------------------------------------------------------------


class TestCheckReply:
    @pytest.mark.asyncio
    async def test_off_sends_nothing(self):
        with patch.object(chat_gate, "_post", new=AsyncMock()) as post:
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.SKIPPED
        post.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message,reply",
        [("", REPLY), (MESSAGE, "  "), (MESSAGE, "x" * (chat_gate.RAW_CHAR_BOUND + 1))],
    )
    async def test_nothing_to_judge_sends_nothing(self, message, reply):
        with (
            override_settings(**ENABLED),
            patch.object(chat_gate, "_post", new=AsyncMock()) as post,
        ):
            result = await chat_gate.check_reply(message, reply, timeout=1.0)
        assert result.outcome == chat_gate.SKIPPED
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_good_reply_passes_and_keeps_its_numbers(self):
        with (
            override_settings(**ENABLED),
            patch.object(chat_gate, "_post", new=AsyncMock(return_value=_payload())),
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.PASS
        assert result.scores == chat_gate.GateScores(0.9, 0.05, 0.05, 0.05, 0.1)
        assert result.scorer == "typesafe/jev-1.13.0/chat-gate-rubric-3"

    @pytest.mark.asyncio
    async def test_a_reply_that_states_a_verdict_fails(self):
        with (
            override_settings(**ENABLED),
            patch.object(
                chat_gate, "_post", new=AsyncMock(return_value=_payload(verdict=0.8))
            ),
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.FAIL
        assert result.scores is not None and result.scores.verdict == 0.8

    @pytest.mark.asyncio
    async def test_a_reply_jev_reads_as_a_promise_fails(self):
        with (
            override_settings(**ENABLED),
            patch.object(
                chat_gate, "_post", new=AsyncMock(return_value=_payload(promises=0.8))
            ),
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.FAIL
        assert result.scores is not None and result.scores.promises == 0.8

    @pytest.mark.asyncio
    async def test_the_request_carries_the_redacted_state_and_the_questions(self):
        sent = {}

        async def fake_ask(state, questions, *, timeout_seconds, use):
            sent.update(
                state=state, questions=questions, timeout=timeout_seconds, use=use
            )
            return _payload()

        with (
            override_settings(**ENABLED),
            patch.object(typesafe, "ask", new=fake_ask),
        ):
            await chat_gate.check_reply(
                MESSAGE,
                REPLY,
                identifiers=[("Pat Doe", "PATIENT#p")],
                timeout=1.0,
            )
        assert sent["questions"] is chat_gate.QUESTIONS
        assert sent["timeout"] == 1.0
        # Counted against TypeSafe's chat budget.
        assert sent["use"] == chat_gate.spend.CHAT
        assert "pat.doe@example.com" not in sent["state"]
        assert "415-555-0100" not in sent["state"]
        assert "Here is how to appeal an MRI denial" in sent["state"]

    @pytest.mark.asyncio
    async def test_no_answer_in_time_is_a_timeout(self):
        async def slow(state, timeout):
            await asyncio.sleep(5)
            return _payload()

        with override_settings(**ENABLED), patch.object(chat_gate, "_post", new=slow):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=0.05)
        assert result.outcome == chat_gate.TIMEOUT
        assert result.failure == "timeout"
        assert result.scores is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 422, 429, 500, 529])
    async def test_an_http_error_is_an_error(self, status):
        error = typesafe.TypeSafeError(f"HTTP {status}", status=status)
        with (
            override_settings(**ENABLED),
            patch.object(chat_gate, "_post", new=AsyncMock(side_effect=error)),
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.ERROR
        assert result.failure == f"HTTP {status}"
        assert result.scores is None

    @pytest.mark.asyncio
    async def test_an_answer_we_cannot_read_is_an_error(self):
        with (
            override_settings(**ENABLED),
            patch.object(
                chat_gate, "_post", new=AsyncMock(return_value={"answers": "?"})
            ),
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.ERROR
        assert result.failure == "ChatGateError"

    @pytest.mark.asyncio
    async def test_failures_log_class_names_only(self, log_capture):
        leaky = RuntimeError(f"upstream echoed: {MESSAGE} {REPLY}")
        with (
            override_settings(**ENABLED),
            patch.object(chat_gate, "_post", new=AsyncMock(side_effect=leaky)),
            log_capture() as cap,
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.ERROR
        assert result.failure == "RuntimeError"
        assert cap.records
        _no_text([r["message"] for r in cap.records], MESSAGE, REPLY)


# --- The chat side: one turn's check ----------------------------------------


def _gate(**kwargs):
    return reply_gate.ReplyGate(
        "chat-id",
        max_wait_seconds=kwargs.get("max_wait", 8.0),
        timeout_seconds=kwargs.get("timeout", 1.0),
    )


def _no_identifiers():
    return patch.object(reply_gate, "_aredactions", new=AsyncMock(return_value=[]))


def _cool_typesafe_down():
    """Start TypeSafe's process-wide cooldown the way a 401 does (the
    conftest ends it after each test)."""
    with override_settings(FHI_TYPESAFE_COOLDOWN_SECONDS=900):
        typesafe._start_cooldown("answered HTTP 401", 401)


class TestGateForTurn:
    def test_on_only_when_everything_holds(self):
        selectable = lambda: True  # noqa: E731
        with override_settings(**ENABLED):
            assert (
                reply_gate.gate_for_turn(
                    "c",
                    external_allowed=True,
                    typed_message=True,
                    ours_selectable=selectable,
                )
                is not None
            )

    @pytest.mark.parametrize(
        "settings_,external,typed,ours",
        [
            (
                dict(TYPESAFE_API_KEY="k", FHI_CHAT_JEV_GATE_ENABLED=False),
                True,
                True,
                True,
            ),
            (
                dict(TYPESAFE_API_KEY=None, FHI_CHAT_JEV_GATE_ENABLED=True),
                True,
                True,
                True,
            ),
            (ENABLED, False, True, True),
            (ENABLED, True, False, True),
            (ENABLED, True, True, False),
        ],
    )
    def test_off_when_anything_does_not(self, settings_, external, typed, ours):
        with override_settings(**settings_):
            assert (
                reply_gate.gate_for_turn(
                    "c",
                    external_allowed=external,
                    typed_message=typed,
                    ours_selectable=lambda: ours,
                )
                is None
            )

    def test_off_while_typesafe_cools_down(self):
        """Every check would fail at once and start the outside calls: the
        turn routes by the policy instead, as with a spent budget."""
        _cool_typesafe_down()
        with override_settings(**ENABLED), patch.object(
            chat_gate, "budget_allows", return_value=True
        ):
            assert (
                reply_gate.gate_for_turn(
                    "c",
                    external_allowed=True,
                    typed_message=True,
                    ours_selectable=lambda: True,
                )
                is None
            )

    def test_the_router_is_not_asked_when_the_check_is_off(self):
        asked = []
        reply_gate.gate_for_turn(
            "c",
            external_allowed=True,
            typed_message=True,
            ours_selectable=lambda: asked.append(1) or True,
        )
        assert asked == []

    def test_the_gate_carries_the_knobs(self):
        with override_settings(
            FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS=4.0,
            FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS=0.5,
            **ENABLED,
        ):
            gate = reply_gate.gate_for_turn(
                "c",
                external_allowed=True,
                typed_message=True,
                ours_selectable=lambda: True,
            )
        assert gate is not None
        assert (gate.max_wait_seconds, gate.timeout_seconds) == (4.0, 0.5)
        assert gate.demote_failed is True

    def test_the_gate_carries_the_demotion_setting(self):
        with override_settings(FHI_CHAT_JEV_GATE_DEMOTE_FAILED=False, **ENABLED):
            gate = reply_gate.gate_for_turn(
                "c",
                external_allowed=True,
                typed_message=True,
                ours_selectable=lambda: True,
            )
        assert gate is not None
        assert gate.demote_failed is False


class TestDemotionRule:
    """Only a fail demotes our reply: an error, a timeout or a reply that
    was never judged says nothing about the reply."""

    @pytest.mark.parametrize(
        "outcome,wanted",
        [
            (chat_gate.FAIL, True),
            (chat_gate.PASS, False),
            (chat_gate.ERROR, False),
            (chat_gate.TIMEOUT, False),
            (chat_gate.SKIPPED, False),
            ("", False),
        ],
    )
    def test_only_a_fail(self, outcome, wanted):
        gate = _gate()
        gate.outcome = outcome
        assert gate.wants_demotion() is wanted

    def test_never_with_the_setting_off(self):
        gate = reply_gate.ReplyGate(
            "c", max_wait_seconds=8.0, timeout_seconds=1.0, demote_failed=False
        )
        gate.outcome = chat_gate.FAIL
        assert gate.wants_demotion() is False


class TestReplyGate:
    @pytest.mark.asyncio
    async def test_a_pass_is_true_and_keeps_numbers_scorer_time_and_model(self):
        gate = _gate()
        gate.used = True
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=AsyncMock(return_value=_payload())),
        ):
            assert (await gate.judge(MESSAGE, REPLY, "fhi-local")).passed is True
        gate.finish()
        assert gate.outcome == chat_gate.PASS
        assert gate.scores == chat_gate.GateScores(0.9, 0.05, 0.05, 0.05, 0.1)
        assert gate.scorer == "typesafe/jev-1.13.0/chat-gate-rubric-3"
        assert gate.model == "fhi-local"
        assert isinstance(gate.ms, int)

    @pytest.mark.asyncio
    async def test_a_cooldown_skips_the_ranking(self):
        """Nothing would be sent: skipped, not an error to note on health."""
        gate = _gate()
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(
                chat_gate, "_post", new=AsyncMock(return_value=_payload(answers=0.8))
            ),
        ):
            await gate.judge(MESSAGE, REPLY, "fhi-local")
        _cool_typesafe_down()
        rank = AsyncMock()
        with (
            override_settings(**ENABLED),
            patch.object(chat_gate, "rank_replies", new=rank),
            patch.object(chat_gate, "budget_allows", return_value=True),
        ):
            result = await gate.rank(MESSAGE, [REPLY, REPLY + " Anything else?"])
        assert result.outcome == chat_gate.SKIPPED
        rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_fail_is_false(self):
        gate = _gate()
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(
                chat_gate, "_post", new=AsyncMock(return_value=_payload(answers=0.2))
            ),
        ):
            assert (await gate.judge(MESSAGE, REPLY, "fhi-local")).passed is False
        assert gate.outcome == chat_gate.FAIL

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            TOOL_REPLY,
            "I can help with that. " + TOOL_REPLY,
            DELETE_DATA_SENTINEL,
            "",
            "   ",
            None,
        ],
    )
    async def test_a_reply_the_person_would_not_see_as_is_is_not_sent(self, reply):
        gate = _gate()
        post = AsyncMock(return_value=_payload())
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(MESSAGE, reply, "fhi-local")).passed is False
        post.assert_not_called()
        assert gate.outcome == chat_gate.SKIPPED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message,reply",
        [
            (MESSAGE, PROMISE_REPLY + " " + TOOL_REPLY),
            (MESSAGE, "🐼"),
            (MESSAGE, PROMISE_REPLY + " " + DELETE_DATA_SENTINEL),
            (MESSAGE, PROMISE_REPLY + " " + "x" * chat_gate.RAW_CHAR_BOUND),
            ("", SHORT_REPLY),
            ("x" * (chat_gate.RAW_CHAR_BOUND + 1), PROMISE_REPLY),
        ],
        ids=[
            "tool-call",
            "summary-marker",
            "delete-handoff",
            "reply-over-bound",
            "empty-message",
            "message-over-bound",
        ],
    )
    async def test_the_skips_come_before_our_own_checks(self, message, reply):
        """Each reply here would fail our own checks, and is still skipped:
        the person would not see it as it is, or Jev could not be sent the
        turn. The retry holds a delivered reply to the same rule."""
        assert chat_gate.fails_our_checks(reply)
        gate = _gate()
        post = AsyncMock(return_value=_payload())
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(message, reply, "fhi-local")).passed is False
        post.assert_not_called()
        assert gate.outcome == chat_gate.SKIPPED
        assert gate.scorer == ""
        assert gate.wants_demotion() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", [PROMISE_REPLY, SHORT_REPLY])
    async def test_a_reply_our_own_checks_reject_fails_without_being_sent(
        self, reply
    ):
        gate = _gate()
        lookup = AsyncMock(return_value=[])
        post = AsyncMock(return_value=_payload())
        with (
            override_settings(**ENABLED),
            patch.object(reply_gate, "_aredactions", new=lookup),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(MESSAGE, reply, "fhi-local")).passed is False
        lookup.assert_not_called()
        post.assert_not_called()
        assert gate.outcome == chat_gate.FAIL
        assert gate.scorer == chat_gate.LOCAL_SCORER == "fhi/local-checks-1"
        assert gate.scores is None
        assert gate.wants_demotion() is True
        # Nothing reached TypeSafe, so its health is not noted.
        assert gate._health is None

    @pytest.mark.asyncio
    async def test_our_own_checks_hold_while_typesafe_is_failing(self):
        gate = _gate()
        post = AsyncMock(side_effect=typesafe.TypeSafeError("HTTP 503", status=503))
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(MESSAGE, PROMISE_REPLY, "fhi-local")).passed is False
        post.assert_not_called()
        assert gate.outcome == chat_gate.FAIL
        assert gate.wants_demotion() is True

    @pytest.mark.asyncio
    async def test_no_identifier_list_means_nothing_is_sent(self):
        gate = _gate()
        post = AsyncMock(return_value=_payload())
        with (
            override_settings(**ENABLED),
            patch.object(
                reply_gate,
                "_aredactions",
                new=AsyncMock(side_effect=OngoingChat.DoesNotExist),
            ),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(MESSAGE, REPLY, "fhi-local")).passed is False
        post.assert_not_called()
        assert gate.outcome == chat_gate.ERROR

    @pytest.mark.asyncio
    async def test_one_check_per_turn(self):
        gate = _gate()
        post = AsyncMock(return_value=_payload())
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(MESSAGE, REPLY, "fhi-local")).passed is True
            assert (await gate.judge(MESSAGE, REPLY, "fhi-local")).passed is False
        assert post.await_count == 1

    @pytest.mark.asyncio
    async def test_a_check_cut_off_by_the_hold_is_a_timeout(self):
        gate = _gate()
        gate.used = True

        async def slow(state, timeout):
            await asyncio.sleep(5)
            return _payload()

        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=slow),
        ):
            task = asyncio.create_task(gate.judge(MESSAGE, REPLY, "fhi-local"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        gate.finish()
        assert gate.outcome == chat_gate.TIMEOUT
        assert gate.scores is None
        assert isinstance(gate.ms, int)

    def test_a_gate_that_never_judged_is_skipped(self):
        gate = _gate()
        gate.used = True
        gate.finish()
        assert gate.outcome == chat_gate.SKIPPED
        assert gate.ms is None

    def test_an_unused_gate_records_nothing(self):
        gate = _gate()
        gate.finish()
        assert gate.outcome == ""

    @pytest.mark.asyncio
    async def test_the_gate_holds_no_text(self):
        gate = _gate()
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=AsyncMock(return_value=_payload())),
        ):
            await gate.judge(MESSAGE, REPLY, "fhi-local")
        _no_text(vars(gate).values(), MESSAGE, REPLY)


@pytest.mark.django_db(transaction=True)
class TestHealthRecord:
    async def _judge_with(self, post):
        gate = _gate()
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=post),
        ):
            await gate.judge(MESSAGE, REPLY, "fhi-local")
        await gate.anote_health()
        return await sync_to_async(
            ExternalServiceHealth.objects.filter(service=chat_gate.SERVICE).first
        )()

    @pytest.mark.asyncio
    async def test_an_answer_notes_a_success(self):
        health = await self._judge_with(AsyncMock(return_value=_payload(answers=0.1)))
        assert health is not None
        assert health.last_success_at is not None
        assert health.last_failure_at is None

    @pytest.mark.asyncio
    async def test_an_http_error_notes_its_status(self):
        error = typesafe.TypeSafeError("HTTP 503", status=503)
        health = await self._judge_with(AsyncMock(side_effect=error))
        assert health is not None
        assert health.last_failure == "HTTP 503"
        assert health.last_success_at is None

    @pytest.mark.asyncio
    async def test_nothing_sent_notes_nothing(self):
        gate = _gate()
        with override_settings(**ENABLED):
            await gate.judge(MESSAGE, TOOL_REPLY, "fhi-local")
        await gate.anote_health()
        assert not await sync_to_async(
            ExternalServiceHealth.objects.filter(service=chat_gate.SERVICE).exists
        )()

    @pytest.mark.asyncio
    async def test_a_reply_our_own_checks_reject_notes_nothing(self):
        gate = _gate()
        post = AsyncMock(return_value=_payload())
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=post),
        ):
            await gate.judge(MESSAGE, PROMISE_REPLY, "fhi-local")
        await gate.anote_health()
        post.assert_not_called()
        assert gate.outcome == chat_gate.FAIL
        assert not await sync_to_async(
            ExternalServiceHealth.objects.filter(service=chat_gate.SERVICE).exists
        )()


async def _until_finished(name):
    for _ in range(200):
        if isolated_db.running(name) == 0:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{name} threads still running")


@pytest.mark.django_db(transaction=True)
class TestDatabaseStepsStayOffTheChatsExecutor:
    """The identifier lookup and the health note run on threads of their
    own (chat/isolated_db.py). A query stuck behind a lock must hold up
    neither the check, which gives up after its timeout, nor the chat's
    thread-sensitive executor, where the rest of the turn and every later
    turn run their ORM calls."""

    @pytest.mark.asyncio
    async def test_a_stuck_lookup_times_out_and_the_chats_next_orm_call_runs(self):
        await OngoingChat.objects.acreate(chat_history=[], summary_for_next_call=[])
        entered, release = threading.Event(), threading.Event()

        def stuck(chat_id):
            entered.set()
            release.wait(10)
            return []

        gate = _gate(timeout=0.2)
        post = AsyncMock(return_value=_payload())
        # The socket's own executor, as PerConnectionThreadSensitiveMixin
        # sets up for a chat.
        async with ThreadSensitiveContext():
            try:
                with (
                    override_settings(**ENABLED),
                    patch.object(reply_gate, "chat_redactions", new=stuck),
                    patch.object(chat_gate, "_post", new=post),
                ):
                    started = time.monotonic()
                    passed = await asyncio.wait_for(
                        gate.judge(MESSAGE, REPLY, "fhi-local"), 5
                    )
                    waited = time.monotonic() - started
                    count = await asyncio.wait_for(OngoingChat.objects.acount(), 2)
                    still_stuck = entered.is_set() and not release.is_set()
            finally:
                release.set()
        await _until_finished(reply_gate.LOOKUP_THREAD)

        assert passed.passed is False
        assert gate.outcome == chat_gate.TIMEOUT
        post.assert_not_called()
        assert waited < 1.0
        assert count == 1
        assert still_stuck

    @pytest.mark.asyncio
    async def test_a_stuck_health_note_is_left_behind_and_the_chats_next_orm_call_runs(
        self,
    ):
        await OngoingChat.objects.acreate(chat_history=[], summary_for_next_call=[])
        entered, release = threading.Event(), threading.Event()

        def stuck(health):
            entered.set()
            release.wait(10)

        gate = _gate()
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=AsyncMock(return_value=_payload())),
        ):
            await gate.judge(MESSAGE, REPLY, "fhi-local")
        async with ThreadSensitiveContext():
            try:
                with (
                    patch.object(reply_gate, "_note_health", new=stuck),
                    patch.object(reply_gate, "HEALTH_NOTE_SECONDS", 0.2),
                ):
                    started = time.monotonic()
                    await asyncio.wait_for(gate.anote_health(), 5)
                    waited = time.monotonic() - started
                    count = await asyncio.wait_for(OngoingChat.objects.acount(), 2)
                    still_stuck = entered.is_set() and not release.is_set()
            finally:
                release.set()
        await _until_finished(reply_gate.HEALTH_THREAD)

        assert waited < 1.0
        assert count == 1
        assert still_stuck

    @pytest.mark.asyncio
    async def test_no_lookup_starts_while_too_many_are_still_running(self):
        lookup = MagicMock(return_value=[])
        post = AsyncMock(return_value=_payload())
        gate = _gate()
        with (
            override_settings(**ENABLED),
            patch.object(
                isolated_db,
                "running",
                new=lambda name: (
                    reply_gate.MAX_RUNNING if name == reply_gate.LOOKUP_THREAD else 0
                ),
            ),
            patch.object(reply_gate, "chat_redactions", new=lookup),
            patch.object(chat_gate, "_post", new=post),
        ):
            assert (await gate.judge(MESSAGE, REPLY, "fhi-local")).passed is False
        lookup.assert_not_called()
        post.assert_not_called()
        assert gate.outcome == chat_gate.ERROR

    @pytest.mark.asyncio
    async def test_no_health_note_starts_while_too_many_are_still_running(self):
        note = MagicMock()
        gate = _gate()
        with (
            override_settings(**ENABLED),
            _no_identifiers(),
            patch.object(chat_gate, "_post", new=AsyncMock(return_value=_payload())),
        ):
            await gate.judge(MESSAGE, REPLY, "fhi-local")
        with (
            patch.object(
                isolated_db,
                "running",
                new=lambda name: (
                    reply_gate.MAX_RUNNING if name == reply_gate.HEALTH_THREAD else 0
                ),
            ),
            patch.object(reply_gate, "_note_health", new=note),
        ):
            await gate.anote_health()
        note.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_lookup_reads_committed_rows_on_its_own_thread(self):
        from django.contrib.auth import get_user_model

        user = await sync_to_async(get_user_model().objects.create_user)(
            username="own-thread", password="x", first_name="Wren", last_name="Tally"
        )
        chat = await OngoingChat.objects.acreate(
            user=user, chat_history=[], summary_for_next_call=[]
        )
        threads = []

        def lookup(chat_id):
            threads.append(threading.get_ident())
            return redaction.chat_redactions(chat_id)

        with patch.object(reply_gate, "chat_redactions", new=lookup):
            found = await reply_gate._aredactions(chat.id, 1.5)
        assert {"Wren", "Tally"} <= {value for value, _category in found}
        assert threads and threads[0] != threading.get_ident()
        await _until_finished(reply_gate.LOOKUP_THREAD)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_check_uses_the_shared_chat_redactions():
    """The check redacts with the shared list in chat/redaction.py
    (test_chat_redaction.py covers it), which holds a signed-in person's
    own names and login."""
    from django.contrib.auth import get_user_model

    assert reply_gate.chat_redactions is redaction.chat_redactions
    User = get_user_model()
    user = await sync_to_async(User.objects.create_user)(
        username="redact-me",
        password="x",
        email="redact-me@example.com",
        first_name="Robin",
        last_name="Quill",
    )
    chat = await sync_to_async(OngoingChat.objects.create)(
        user=user, chat_history=[], summary_for_next_call=[]
    )
    found = await sync_to_async(redaction.chat_redactions)(chat.id)
    values = {value for value, _category in found}
    assert {"Robin", "Quill", "redact-me@example.com", "redact-me"} <= values


# --- The turn row -----------------------------------------------------------


class _Named:
    def __init__(self, name):
        self._name = name

    def __str__(self):
        return self._name


def test_a_turn_without_a_check_says_so():
    fields = TurnRecord.start(True, [_Named("a")]).row_fields("ok")
    assert fields["gate_used"] is False
    assert fields["gate_outcome"] == ""
    assert (
        fields["gate_answers"],
        fields["gate_verdict"],
        fields["gate_asks_again"],
        fields["gate_promises"],
    ) == (
        None,
        None,
        None,
        None,
    )
    assert (fields["gate_scorer"], fields["gate_ms"], fields["gate_model"]) == (
        "",
        None,
        "",
    )


def test_the_row_carries_the_check_numbers_only():
    turn = TurnRecord.start(True, [_Named("a")])
    turn.set_gate(
        chat_gate.FAIL,
        (0.4, 0.1, float("nan"), 0.85),
        "typesafe/jev-1.13.0/chat-gate-rubric-3",
        412,
        "fhi-local",
    )
    fields = turn.row_fields("ok")
    assert fields["gate_used"] is True
    assert fields["gate_outcome"] == "fail"
    assert (fields["gate_answers"], fields["gate_verdict"]) == (0.4, 0.1)
    assert fields["gate_promises"] == 0.85
    # Postgres jsonb and float columns take no NaN: a non-finite answer is null.
    assert fields["gate_asks_again"] is None
    assert fields["gate_ms"] == 412
    assert fields["gate_model"] == "fhi-local"


def test_the_row_records_a_fail_by_our_own_checks():
    turn = TurnRecord.start(True, [_Named("a")])
    turn.set_gate(chat_gate.FAIL, None, chat_gate.LOCAL_SCORER, 3, "fhi-local")
    fields = turn.row_fields("ok")
    assert fields["gate_outcome"] == "fail"
    assert fields["gate_scorer"] == "fhi/local-checks-1"
    assert fields["gate_promises"] is None
    assert fields["gate_answers"] is None


def test_the_row_records_the_demotion():
    turn = TurnRecord.start(True, [_Named("a")])
    fields = turn.row_fields("ok")
    assert (fields["gate_demoted"], fields["gate_demoted_delivered"]) == (
        False,
        False,
    )
    turn.set_gate_demotion(True, False)
    fields = turn.row_fields("ok")
    assert (fields["gate_demoted"], fields["gate_demoted_delivered"]) == (True, False)
    turn.set_gate_demotion(True, True)
    fields = turn.row_fields("ok")
    assert (fields["gate_demoted"], fields["gate_demoted_delivered"]) == (True, True)
    # Delivered as demoted only when it was demoted.
    turn.set_gate_demotion(False, True)
    fields = turn.row_fields("ok")
    assert (fields["gate_demoted"], fields["gate_demoted_delivered"]) == (
        False,
        False,
    )


def test_the_outcomes_agree_with_the_model():
    assert set(ChatTurn.GateOutcome.values) == {""} | set(chat_gate.OUTCOMES)

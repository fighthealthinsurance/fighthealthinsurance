"""The reply check's tiers (ml/chat_gate.py, chat/reply_gate.py): a clear
pass keeps the outside models out, a borderline reply asks them and has Jev
rank every candidate, a fail asks them and demotes ours, and a crucial
moment starts the reserved side-by-side call. Anything but an answer from
Jev leaves the race's default and never starts the reserved call.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from django.test import override_settings

from fighthealthinsurance.chat import reply_gate
from fighthealthinsurance.chat.turn_record import (
    ALTERNATE_CRUCIAL,
    CallLog,
    TurnRecord,
)
from fighthealthinsurance.ml import chat_gate, spend, typesafe
from fighthealthinsurance.models import ChatTurn
from fighthealthinsurance.utils import CheckVerdict

ENABLED = dict(TYPESAFE_API_KEY="test-key", FHI_CHAT_JEV_GATE_ENABLED=True)
MESSAGE = "My plan denied my MRI and the appeal deadline is Friday."
REPLY = (
    "Here is how to appeal an MRI denial: ask for the denial letter, then "
    "write to the plan before Friday. Want me to draft the appeal?"
)
OTHER = (
    "You can appeal the MRI denial. Start with the denial letter and the "
    "plan's appeal form, and send it before the Friday deadline."
)


def _scores(answers=0.9, verdict=0.05, asks_again=0.05, promises=0.05, crucial=None):
    return chat_gate.GateScores(answers, verdict, asks_again, promises, crucial)


def _payload(answers=0.9, problem=0.05, crucial=0.1):
    return {
        "model": "jev-1.13.0",
        "answers": {
            chat_gate.ANSWERS_QUESTION: {"type": "noul", "noul": answers},
            chat_gate.STATES_VERDICT: {"type": "noul", "noul": problem},
            chat_gate.ASKS_AGAIN: {"type": "noul", "noul": problem},
            chat_gate.PROMISES_OUTCOME: {"type": "noul", "noul": problem},
            chat_gate.CRUCIAL_MOMENT: {"type": "noul", "noul": crucial},
        },
    }


def _rank_payload(*per_reply):
    """per_reply: (answers, problem) for REPLY 1, 2, ..."""
    answers = {}
    for k, (a, problem) in enumerate(per_reply, start=1):
        answers[f"{chat_gate.ANSWERS_QUESTION}_{k}"] = {"noul": a}
        answers[f"{chat_gate.STATES_VERDICT}_{k}"] = {"noul": problem}
        answers[f"{chat_gate.ASKS_AGAIN}_{k}"] = {"noul": problem}
        answers[f"{chat_gate.PROMISES_OUTCOME}_{k}"] = {"noul": problem}
    return {"model": "jev-1.13.0", "answers": answers}


# --- Tiers ------------------------------------------------------------------


class TestTiers:
    def test_defaults(self):
        assert chat_gate.clear_answers() == 0.85
        assert chat_gate.clear_problem() == 0.15
        assert chat_gate.crucial_threshold() == 0.5
        assert chat_gate.rank_timeout_seconds() == 3.0

    @pytest.mark.parametrize(
        "scores,expected",
        [
            (_scores(0.9, 0.05, 0.05, 0.05), chat_gate.PASS),
            (_scores(0.85, 0.14, 0.0, 0.0), chat_gate.PASS),
            # Above the fail line, short of the clear one.
            (_scores(0.8, 0.05, 0.05, 0.05), chat_gate.BORDERLINE),
            (_scores(0.95, 0.2, 0.0, 0.0), chat_gate.BORDERLINE),
            (_scores(0.9, 0.0, 0.15, 0.0), chat_gate.BORDERLINE),
            # The fail line stays where it was.
            (_scores(0.69, 0.0, 0.0, 0.0), chat_gate.FAIL),
            (_scores(1.0, 0.0, 0.0, 0.3), chat_gate.FAIL),
        ],
    )
    def test_the_three_tiers(self, scores, expected):
        assert chat_gate.tier(scores) == expected

    def test_a_clear_line_looser_than_the_fail_line_never_passes_a_fail(self):
        with override_settings(
            FHI_CHAT_JEV_GATE_CLEAR_ANSWERS=0.1, FHI_CHAT_JEV_GATE_CLEAR_PROBLEM=0.9
        ):
            assert chat_gate.tier(_scores(0.5, 0.0, 0.0, 0.0)) == chat_gate.FAIL
            assert chat_gate.tier(_scores(0.7, 0.29, 0.0, 0.0)) == chat_gate.PASS

    def test_crucial_reads_its_own_answer(self):
        assert chat_gate.is_crucial(_scores(crucial=0.5))
        assert not chat_gate.is_crucial(_scores(crucial=0.49))
        assert not chat_gate.is_crucial(_scores(crucial=None))
        assert not chat_gate.is_crucial(None)
        with override_settings(FHI_CHAT_JEV_CRUCIAL_MIN=0.8):
            assert not chat_gate.is_crucial(_scores(crucial=0.7))

    def test_quality_discounts_the_likeliest_problem(self):
        assert chat_gate.quality(_scores(1.0, 0.0, 0.0, 0.0)) == 1.0
        assert chat_gate.quality(_scores(0.8, 0.5, 0.1, 0.0)) == pytest.approx(0.4)
        assert chat_gate.quality(_scores(0.0, 0.0, 0.0, 0.0)) == 0.0

    @pytest.mark.asyncio
    async def test_check_reply_returns_the_tier(self):
        with override_settings(**ENABLED), patch.object(
            chat_gate, "_post", new=AsyncMock(return_value=_payload(answers=0.8))
        ):
            result = await chat_gate.check_reply(MESSAGE, REPLY, timeout=1.0)
        assert result.outcome == chat_gate.BORDERLINE
        assert result.answered


# --- The ranking request ------------------------------------------------------


class TestRankRequest:
    def test_the_questions_point_at_each_reply_by_number(self):
        questions = chat_gate.rank_questions(3)
        assert len(questions) == 12
        assert chat_gate.CRUCIAL_MOMENT not in {k.rsplit("_", 1)[0] for k in questions}
        second = questions[f"{chat_gate.PROMISES_OUTCOME}_2"]
        assert "THE REPLY 2" in second["instructions"]
        assert "THE REPLY 1" not in second["instructions"]
        assert second["type"] == "noul"

    def test_the_state_redacts_and_keeps_every_reply_under_the_cap(self):
        redactor = chat_gate.letter_quality.Redactor([("Pat Doe", "PATIENT#p")])
        state = chat_gate.build_rank_state(
            "I am Pat Doe, 415-555-0100.",
            ["Pat Doe, call 415-555-0100.", "R" * 20_000, "S" * 20_000],
            redactor,
        )
        assert len(state) <= chat_gate.STATE_CHAR_CAP
        for secret in ("Pat Doe", "415-555-0100"):
            assert secret not in state
        for k in (1, 2, 3):
            assert f"THE REPLY {k}:" in state
        # Each long reply got its share; neither pushed the other out.
        assert "RRRR" in state and "SSSS" in state

    def test_parsing_is_strict(self):
        payload = _rank_payload((0.9, 0.1), (0.5, 0.0))
        scores = chat_gate.parse_rank(payload, 2)
        assert [s.answers for s in scores] == [0.9, 0.5]
        with pytest.raises(chat_gate.ChatGateError):
            chat_gate.parse_rank(payload, 3)
        del payload["answers"][f"{chat_gate.ASKS_AGAIN}_2"]
        with pytest.raises(chat_gate.ChatGateError):
            chat_gate.parse_rank(payload, 2)

    @pytest.mark.asyncio
    async def test_jev_picks_the_best_and_ties_keep_the_given_order(self):
        post = AsyncMock(return_value=_rank_payload((0.7, 0.1), (0.9, 0.0), (0.9, 0.0)))
        with override_settings(**ENABLED), patch.object(chat_gate, "_post", new=post):
            result = await chat_gate.rank_replies(
                MESSAGE, [REPLY, OTHER, OTHER + " Thanks."], timeout=1.0
            )
        assert result.outcome == chat_gate.RANK_PICKED
        assert result.best == 1
        assert result.order() == [1, 2, 0]
        state, timeout, questions = post.await_args.args
        assert set(questions) == set(chat_gate.rank_questions(3))

    @pytest.mark.asyncio
    async def test_it_is_counted_against_the_chat_budget(self):
        sent = {}

        async def fake_ask(state, questions, *, timeout_seconds, use):
            sent["use"] = use
            return _rank_payload((0.9, 0.0), (0.8, 0.0))

        with override_settings(**ENABLED), patch.object(typesafe, "ask", new=fake_ask):
            await chat_gate.rank_replies(MESSAGE, [REPLY, OTHER], timeout=1.0)
        assert sent["use"] == spend.CHAT

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "replies",
        [[REPLY], [REPLY] * (chat_gate.MAX_RANKED + 1), [REPLY, ""], [REPLY, "x" * 10**6]],
    )
    async def test_nothing_is_sent_without_two_to_four_judgeable_replies(self, replies):
        post = AsyncMock()
        with override_settings(**ENABLED), patch.object(chat_gate, "_post", new=post):
            result = await chat_gate.rank_replies(MESSAGE, replies, timeout=1.0)
        assert result.outcome == chat_gate.SKIPPED
        post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nothing_is_sent_with_the_gate_off(self):
        post = AsyncMock()
        with patch.object(chat_gate, "_post", new=post):
            result = await chat_gate.rank_replies(MESSAGE, [REPLY, OTHER], timeout=1.0)
        assert result.outcome == chat_gate.SKIPPED
        post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_timeout_or_error_picks_nothing(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        with override_settings(**ENABLED), patch.object(chat_gate, "_post", new=slow):
            result = await chat_gate.rank_replies(MESSAGE, [REPLY, OTHER], timeout=0.05)
        assert result.outcome == chat_gate.TIMEOUT
        assert result.best is None and result.order() == []
        with override_settings(**ENABLED), patch.object(
            chat_gate, "_post", new=AsyncMock(return_value={"answers": {}})
        ):
            result = await chat_gate.rank_replies(MESSAGE, [REPLY, OTHER], timeout=1.0)
        assert result.outcome == chat_gate.ERROR


# --- The gate's verdicts ----------------------------------------------------


OUTSIDE = ("outside-1", "outside-2")
RESERVED = ("kimi",)


def _gate():
    gate = reply_gate.ReplyGate("chat-id", max_wait_seconds=8.0, timeout_seconds=1.0)
    gate.bind(OUTSIDE, RESERVED)
    return gate


def _no_identifiers():
    return patch.object(reply_gate, "_aredactions", new=AsyncMock(return_value=[]))


async def _judge(payload, reply=REPLY):
    gate = _gate()
    with (
        override_settings(**ENABLED),
        _no_identifiers(),
        patch.object(chat_gate, "_post", new=AsyncMock(return_value=payload)),
    ):
        verdict = await gate.judge(MESSAGE, reply, "fhi-local")
    return gate, verdict


class TestVerdicts:
    @pytest.mark.asyncio
    async def test_a_clear_pass_starts_nothing(self):
        gate, verdict = await _judge(_payload())
        assert verdict == CheckVerdict(passed=True, start=())
        assert not gate.wants_rank() and not gate.crucial

    @pytest.mark.asyncio
    async def test_a_crucial_clear_pass_starts_only_the_side_by_side(self):
        gate, verdict = await _judge(_payload(crucial=0.9))
        assert verdict == CheckVerdict(passed=True, start=RESERVED)
        assert gate.crucial

    @pytest.mark.asyncio
    async def test_a_borderline_reply_starts_the_outside_models_and_is_ranked(self):
        gate, verdict = await _judge(_payload(answers=0.8))
        assert verdict == CheckVerdict(passed=False, start=OUTSIDE)
        assert gate.wants_rank()
        # Borderline is not a fail: ours is not demoted.
        assert not gate.wants_demotion()

    @pytest.mark.asyncio
    async def test_a_crucial_borderline_reply_adds_the_side_by_side(self):
        _gate_, verdict = await _judge(_payload(answers=0.8, crucial=0.6))
        assert verdict == CheckVerdict(passed=False, start=OUTSIDE + RESERVED)

    @pytest.mark.asyncio
    async def test_a_fail_starts_the_outside_models_and_demotes(self):
        gate, verdict = await _judge(_payload(answers=0.2, crucial=0.9))
        assert verdict == CheckVerdict(passed=False, start=OUTSIDE + RESERVED)
        assert gate.wants_demotion() and not gate.wants_rank()

    @pytest.mark.asyncio
    async def test_our_own_checks_leave_the_default_and_never_the_side_by_side(self):
        gate, verdict = await _judge(_payload(crucial=0.9), reply="Ok.")
        assert gate.outcome == chat_gate.FAIL
        assert verdict == CheckVerdict(passed=False)

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_leaves_the_default(self):
        gate, verdict = await _judge({"answers": "?"})
        assert gate.outcome == chat_gate.ERROR
        assert verdict == CheckVerdict(passed=False)
        assert not gate.crucial

    def test_a_spent_chat_budget_means_no_gate(self):
        with override_settings(**ENABLED), patch.object(
            chat_gate, "budget_allows", return_value=False
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


class TestGateRanking:
    @pytest.mark.asyncio
    async def test_the_ranking_reuses_the_identifiers_and_runs_once(self):
        gate, _verdict = await _judge(_payload(answers=0.8))
        rank = AsyncMock(
            return_value=chat_gate.RankResult(
                outcome=chat_gate.RANK_PICKED, scores=(_scores(), _scores())
            )
        )
        with override_settings(**ENABLED), patch.object(chat_gate, "rank_replies", new=rank):
            first = await gate.rank(MESSAGE, [REPLY, OTHER])
            second = await gate.rank(MESSAGE, [REPLY, OTHER])
        assert first.outcome == chat_gate.RANK_PICKED
        assert second.outcome == chat_gate.SKIPPED
        rank.assert_awaited_once()
        assert rank.await_args.kwargs["identifiers"] == []
        assert (gate.rank_outcome, gate.rank_count) == (chat_gate.RANK_PICKED, 2)
        assert isinstance(gate.rank_ms, int)

    @pytest.mark.asyncio
    async def test_no_ranking_before_a_check_looked_up_the_identifiers(self):
        gate = _gate()
        rank = AsyncMock()
        with override_settings(**ENABLED), patch.object(chat_gate, "rank_replies", new=rank):
            result = await gate.rank(MESSAGE, [REPLY, OTHER])
        assert result.outcome == chat_gate.SKIPPED
        rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_spent_budget_skips_the_ranking(self):
        gate, _verdict = await _judge(_payload(answers=0.8))
        rank = AsyncMock()
        with (
            override_settings(**ENABLED),
            patch.object(chat_gate, "rank_replies", new=rank),
            patch.object(chat_gate, "budget_allows", return_value=False),
        ):
            result = await gate.rank(MESSAGE, [REPLY, OTHER])
        assert result.outcome == chat_gate.SKIPPED
        assert gate.rank_outcome == chat_gate.SKIPPED
        rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_identifiers_are_dropped_when_the_turn_is_over(self):
        gate, _verdict = await _judge(_payload(answers=0.8))
        assert gate._identifiers == []
        with patch.object(reply_gate.isolated_db, "run_isolated", new=AsyncMock()):
            await gate.anote_health()
        assert gate._identifiers is None


# --- The row ------------------------------------------------------------------


def test_the_row_carries_crucial_the_ranking_and_the_reason():
    turn = TurnRecord.start(use_external=True, primary_models=[])
    turn.set_gate(
        chat_gate.BORDERLINE, (0.8, 0.1, 0.1, 0.1), "typesafe/x", 12, "fhi", 0.7
    )
    turn.set_rank(chat_gate.RANK_PICKED, 900, 3, True)
    turn.set_alternate_candidate("kimi", True, ALTERNATE_CRUCIAL)
    turn.offer_alternate()
    fields = turn.row_fields("ok")
    assert fields["gate_crucial"] == 0.7
    assert (
        fields["rank_outcome"],
        fields["rank_ms"],
        fields["rank_count"],
        fields["rank_changed"],
    ) == (chat_gate.RANK_PICKED, 900, 3, True)
    assert fields["alternate_reason"] == ALTERNATE_CRUCIAL


def test_a_reason_is_kept_only_when_the_side_by_side_is_shown():
    turn = TurnRecord.start(use_external=True, primary_models=[])
    turn.set_alternate_candidate("kimi", True, ALTERNATE_CRUCIAL)
    assert turn.row_fields("ok")["alternate_reason"] == ""


def test_the_outcomes_agree_with_the_model():
    assert set(ChatTurn.RankOutcome.values) == {""} | set(chat_gate.RANK_OUTCOMES)
    assert set(ChatTurn.GateOutcome.values) == {""} | set(chat_gate.OUTCOMES)


@pytest.mark.asyncio
async def test_each_ranked_call_keeps_jevs_score():
    log = CallLog("primary")

    async def reply():
        return (REPLY, None)

    call = log.observe(reply(), "outside-1", "truncated")
    other = log.observe(reply(), "outside-2", "truncated")
    await call
    await other
    rows = log.finish()
    log.note_jev(call, 0.87654)
    log.note_jev(other, float("nan"))
    log.note_jev(object(), 0.5)
    assert rows[0]["jev"] == 0.8765
    assert "jev" not in rows[1]

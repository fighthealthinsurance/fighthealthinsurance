"""The per-turn model-race record (chat/turn_record.py) and the choice of
side-by-side alternate (llm_client.pick_side_by_side_alternate).

A ChatTurn row says which models raced for a chat turn, how each call
ended, which model won, and which side-by-side answer the person picked. It
holds metadata only, so these tests also pin that nothing else gets in.
"""

import asyncio
import inspect
import json
import threading
import time
import uuid

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import connections

from fighthealthinsurance.chat import turn_record
from fighthealthinsurance.chat.llm_client import (
    build_llm_calls,
    build_llm_calls_for_variants,
    build_retry_calls,
    candidates_best_first,
    credit_for_delivered_reply,
    pick_side_by_side_alternate,
)
from fighthealthinsurance.chat.message_preprocessor import MessageVariant
from fighthealthinsurance.chat.turn_record import (
    CALL_STATUSES,
    PASS_PRIMARY,
    PASS_RETRY,
    PASS_TOOL,
    PREFERENCE_LABELS,
    TURN_OUTCOMES,
    CallLog,
    ReplyCredit,
    TurnRecord,
    _bound_statements,
    arecord_answer_preference,
    arecord_chat_turn,
    arecord_chat_turn_isolated,
)
from fighthealthinsurance.ml.ml_metrics import _ANSWER_FEEDBACK_ALLOWED
from fighthealthinsurance.models import ChatTurn, OngoingChat
from fighthealthinsurance.utils import (
    STAGE_OUTCOMES,
    StagedStart,
    best_two_within_timelimit,
)
from tests.chat_fixtures import FRESH_REPLY, SECOND_OPINION_REPLY, RecordingChatModel

# A third distinct, presentable answer (its own wording, so it is not a
# near-duplicate of either fixture reply).
THIRD_REPLY = (
    "One more option to consider: ask your county office for the hardship "
    "exemption form, since caregivers and people with a medical condition "
    "can be excused from the hours rule entirely."
)

CALL_KEYS = {
    "model",
    "backend",
    "external",
    "pass",
    "depth",
    "history",
    "variant",
    "status",
    "error",
    "ms",
    "score",
}


class _Named:
    def __init__(self, name, external=None):
        self._name = name
        if external is not None:
            self.external = external

    def __str__(self):
        return self._name


async def _answer(text, context="a context summary"):
    return (text, context)


async def _raise():
    raise RuntimeError("the patient's plan is Blue Shield")


async def _stall():
    await asyncio.sleep(10)
    return ("never seen", "never seen")


# --- CallLog ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_way_a_call_can_end_gets_its_status():
    log = CallLog(PASS_PRIMARY)
    scored = log.observe(_answer(FRESH_REPLY), _Named("scored-model"), "truncated")
    repeat = log.observe(_answer(SECOND_OPINION_REPLY), _Named("repeat-model"), "full")
    empty = log.observe(_answer(None, None), _Named("empty-model"), "truncated")
    error = log.observe(_raise(), _Named("error-model"), "truncated")
    late = log.observe(_stall(), _Named("late-model"), "truncated")

    def score_fn(result, task):
        if task is scored:
            return 8820.0
        return float("-inf")

    await best_two_within_timelimit(
        [scored, repeat, empty, error, late],
        log.scoring(score_fn),
        timeout=0.3,
        extended_timeout=0.0,
    )
    calls = {c["model"]: c for c in log.finish()}

    assert {m: c["status"] for m, c in calls.items()} == {
        "scored-model": "scored",
        "repeat-model": "repeat",
        "empty-model": "empty",
        "error-model": "error",
        "late-model": "late",
    }
    assert calls["scored-model"]["score"] == 8820.0
    # Rejected scores are stored as null: jsonb refuses -Infinity.
    assert calls["repeat-model"]["score"] is None
    assert calls["empty-model"]["score"] is None
    # The class name only, never the exception text.
    assert calls["error-model"]["error"] == "RuntimeError"
    assert calls["late-model"]["ms"] is None
    assert isinstance(calls["scored-model"]["ms"], int)
    assert calls["repeat-model"]["history"] == "full"
    json.dumps(list(calls.values()), allow_nan=False)


@pytest.mark.asyncio
async def test_a_call_that_answered_before_the_turn_gave_up_keeps_its_time():
    """The race scores only once every call is in (or its window closes).
    When the turn budget ends the race first, the calls that had already
    answered were never scored, but they did finish: they keep their time,
    and only the call still running is late."""
    log = CallLog(PASS_PRIMARY)
    answered = log.observe(_answer(FRESH_REPLY), _Named("answered-model"), "truncated")
    answered_empty = log.observe(
        _answer(None, None), _Named("empty-model"), "truncated"
    )
    stalled = log.observe(_stall(), _Named("stalled-model"), "truncated")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            best_two_within_timelimit(
                [answered, answered_empty, stalled],
                log.scoring(lambda result, task: 1.0),
                timeout=10.0,
            ),
            0.3,
        )
    calls = {c["model"]: c for c in log.finish()}

    assert {m: c["status"] for m, c in calls.items()} == {
        "answered-model": "unscored",
        "empty-model": "empty",
        "stalled-model": "late",
    }
    assert isinstance(calls["answered-model"]["ms"], int)
    assert isinstance(calls["empty-model"]["ms"], int)
    assert calls["stalled-model"]["ms"] is None
    assert calls["answered-model"]["score"] is None
    json.dumps(list(calls.values()), allow_nan=False)


@pytest.mark.asyncio
async def test_a_cancelled_call_stays_late_once_its_cancellation_lands():
    """A race cancels its leftover calls in the background, so a pass may
    finish its log before or after a cancellation lands. Either way the
    call never answered: it stays late, with no time."""
    log = CallLog(PASS_PRIMARY)
    stalled = log.observe(_stall(), _Named("stalled-model"), "truncated")
    task = asyncio.ensure_future(stalled)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (entry,) = log.finish()
    assert (entry["status"], entry["ms"]) == ("late", None)


@pytest.mark.asyncio
async def test_every_call_entry_has_exactly_the_metadata_keys():
    log = CallLog(PASS_PRIMARY)
    call = log.observe(_answer(FRESH_REPLY), _Named("m"), "truncated")
    await best_two_within_timelimit([call], log.scoring(lambda r, t: 1.0), 1.0)
    (entry,) = log.finish()
    assert set(entry) == CALL_KEYS
    for value in entry.values():
        assert FRESH_REPLY not in str(value)


@pytest.mark.asyncio
async def test_a_rejected_retry_call_is_empty_not_a_repeat():
    """The retry scorer hard-rejects only answers too short to deliver, so a
    rejected retry call is empty even when it carried a little text."""
    log = CallLog(PASS_RETRY)
    call = log.observe(_answer("ok."), _Named("m"), "retry_short")
    await best_two_within_timelimit(
        [call], log.scoring(lambda r, t: float("-inf")), 1.0, 0.0
    )
    (entry,) = log.finish()
    assert entry["status"] == "empty"
    assert entry["pass"] == "retry"


@pytest.mark.asyncio
async def test_builders_without_a_log_return_the_backend_calls_unchanged():
    model = RecordingChatModel(always_reply=FRESH_REPLY, name="plain")
    calls, _scores = build_llm_calls(
        model_backends=[model],
        current_message="hi",
        previous_context_summary=None,
        history=[],
        is_professional=False,
        is_logged_in=False,
    )
    assert [c.cr_code.co_name for c in calls] == ["generate_chat_response"]
    await asyncio.gather(*calls)


@pytest.mark.asyncio
async def test_builders_record_history_kind_variant_and_labels():
    model = RecordingChatModel(always_reply=FRESH_REPLY, name="fhi-test")
    fallback = RecordingChatModel(always_reply=FRESH_REPLY, name="fallback-test")
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    full_history = history + history
    log = CallLog(PASS_PRIMARY)
    labels = {}
    calls, _scores, _primary = build_llm_calls_for_variants(
        model_backends=[model],
        variants=[
            MessageVariant(kind="primary_original", text_for_llm="x", score_delta=0),
            MessageVariant(kind="long_summary", text_for_llm="y", score_delta=-10),
        ],
        previous_context_summary=None,
        history=history,
        is_professional=False,
        is_logged_in=False,
        full_history=full_history,
        call_labels=labels,
        call_log=log,
    )
    # Labels are keyed by the wrapped call, which is what the race sees.
    assert set(labels) == set(calls)
    await asyncio.gather(*calls)
    entries = log.finish()
    assert [(e["history"], e["variant"]) for e in entries] == [
        ("truncated", "primary_original"),
        ("full", "primary_original"),
        ("truncated", "long_summary"),
        ("full", "long_summary"),
    ]
    assert {e["model"] for e in entries} == {"fhi-test"}

    retry_log = CallLog(PASS_RETRY)
    retry_calls, _ = build_retry_calls(
        model_backends=[model],
        current_message="x",
        previous_context_summary=None,
        history=history,
        is_professional=False,
        is_logged_in=False,
        fallback_backends=[fallback],
        call_log=retry_log,
    )
    await asyncio.gather(*retry_calls)
    assert [(e["model"], e["history"]) for e in retry_log.finish()] == [
        ("fhi-test", "retry_short"),
        ("fhi-test", "retry_full"),
        ("fallback-test", "retry_short"),
        ("fallback-test", "retry_full"),
    ]


def test_turn_record_knows_which_labels_are_outside_models():
    turn = TurnRecord.start(
        use_external=True,
        primary_models=[_Named("fhi-local", False), _Named("fhi-local", False)],
        fallback_models=[_Named("claude", True), _Named("stub")],
    )
    turn.set_winner("claude", 1900.0, True, None, None, False)
    fields = turn.row_fields("ok")
    assert fields["backends"] == ["fhi-local", "fhi-local"]
    assert fields["fallback_backends"] == ["claude", "stub"]
    assert fields["winner_external"] is True
    assert fields["winner_pass"] == "retry"
    assert fields["retry_used"] is True
    turn.set_winner("stub", float("-inf"), False, None, None, False)
    fields = turn.row_fields("ok")
    assert fields["winner_external"] is None
    assert fields["winner_score"] is None


def test_the_delivered_model_is_the_winner_and_the_first_pass_pick_is_kept():
    """A tool follow-up from another model wrote the reply: the row credits
    that model, and keeps the first pass's pick beside it."""
    turn = TurnRecord.start(
        use_external=True,
        primary_models=[_Named("model-a", False), _Named("model-b", True)],
    )
    turn.set_winner("model-a", 2630.0, False, "model-b", 2400.0, True)
    fields = turn.row_fields("ok")
    assert (fields["winner_model"], fields["first_pass_model"]) == (
        "model-a",
        "model-a",
    )
    assert fields["winner_external"] is False

    turn.set_delivered(ReplyCredit("model-b", 1900.0, PASS_TOOL, True))
    fields = turn.row_fields("ok")
    assert (fields["winner_model"], fields["winner_score"]) == ("model-b", 1900.0)
    assert fields["winner_pass"] == "tool"
    assert fields["winner_external"] is True
    assert fields["retry_used"] is True
    assert (fields["first_pass_model"], fields["first_pass_score"]) == (
        "model-a",
        2630.0,
    )
    assert fields["runner_up_model"] == "model-b"


FIRST_PASS = ReplyCredit("model-a", 2630.0, PASS_PRIMARY, False)
FOLLOW_UP_B = ReplyCredit("model-b", 1900.0, PASS_TOOL, False)
FOLLOW_UP_C = ReplyCredit("model-c", 1800.0, PASS_TOOL, True)


def test_a_follow_up_that_replaced_the_reply_gets_the_credit():
    credit = credit_for_delivered_reply(
        FIRST_PASS, [(FRESH_REPLY, FOLLOW_UP_B)], FRESH_REPLY
    )
    assert credit == FOLLOW_UP_B


def test_a_follow_up_joined_onto_the_reply_gets_the_credit():
    delivered = "Let me look that up for you.\n\n" + FRESH_REPLY
    credit = credit_for_delivered_reply(
        FIRST_PASS, [("\n" + FRESH_REPLY + "\n", FOLLOW_UP_B)], delivered
    )
    assert credit == FOLLOW_UP_B


def test_the_latest_follow_up_in_the_reply_gets_the_credit():
    delivered = FRESH_REPLY + "\n\n" + THIRD_REPLY
    credit = credit_for_delivered_reply(
        FIRST_PASS,
        [(FRESH_REPLY, FOLLOW_UP_B), (THIRD_REPLY, FOLLOW_UP_C)],
        delivered,
    )
    assert credit == FOLLOW_UP_C


def test_a_follow_up_the_tool_did_not_use_leaves_the_pass_its_credit():
    assert (
        credit_for_delivered_reply(
            FIRST_PASS, [(THIRD_REPLY, FOLLOW_UP_B)], FRESH_REPLY
        )
        == FIRST_PASS
    )
    assert credit_for_delivered_reply(FIRST_PASS, [], FRESH_REPLY) == FIRST_PASS
    assert (
        credit_for_delivered_reply(FIRST_PASS, [("  ", FOLLOW_UP_B)], FRESH_REPLY)
        == FIRST_PASS
    )


def test_an_alternate_candidate_counts_only_once_offered():
    turn = TurnRecord.start(True, [_Named("a")])
    turn.set_alternate_candidate("b", True)
    fields = turn.row_fields("ok")
    assert fields["alternate_offered"] is False
    assert fields["alternate_model"] == ""
    assert fields["alternate_cross_model"] is False
    turn.offer_alternate()
    fields = turn.row_fields("ok")
    assert (fields["alternate_model"], fields["alternate_cross_model"]) == ("b", True)


def test_label_sets_agree_with_the_metrics_and_the_model():
    assert PREFERENCE_LABELS == _ANSWER_FEEDBACK_ALLOWED
    assert PREFERENCE_LABELS == set(ChatTurn.Preferred.values) - {""}
    assert TURN_OUTCOMES == set(ChatTurn.Outcome.values)
    assert len(CALL_STATUSES) == len(set(CALL_STATUSES)) == 7
    assert set(ChatTurn.ExternalStart.values) == {""} | set(STAGE_OUTCOMES)


@pytest.mark.asyncio
async def test_a_held_back_call_that_never_starts_is_skipped_and_closed():
    log = CallLog(PASS_PRIMARY)
    ours = log.observe(_answer(FRESH_REPLY), _Named("fhi-local", False), "truncated")
    outside = _answer(SECOND_OPINION_REPLY)
    theirs = log.observe(outside, _Named("claude", True), "truncated")
    stage = StagedStart()
    await best_two_within_timelimit(
        [ours, theirs],
        log.scoring(lambda r, t: 8820.0),
        timeout=1.0,
        deferred=[theirs],
        defer_seconds=5.0,
        stage=stage,
    )
    log.mark_skipped(stage.skipped)
    calls = {c["model"]: c for c in log.finish()}
    assert calls["fhi-local"]["status"] == "scored"
    assert calls["claude"]["status"] == "skipped"
    assert (calls["claude"]["ms"], calls["claude"]["score"]) == (None, None)
    assert calls["claude"]["external"] is True
    # The backend call inside the wrapper is closed too: it will never run.
    assert inspect.getcoroutinestate(outside) == inspect.CORO_CLOSED
    assert inspect.getcoroutinestate(theirs) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_marking_a_call_that_ran_as_skipped_changes_nothing():
    log = CallLog(PASS_PRIMARY)
    call = log.observe(_answer(FRESH_REPLY), _Named("m"), "truncated")
    await best_two_within_timelimit([call], log.scoring(lambda r, t: 1.0), 1.0)
    log.mark_skipped([call])
    (entry,) = log.finish()
    assert entry["status"] == "scored"


def test_the_row_says_how_the_outside_models_were_started():
    turn = TurnRecord.start(True, [_Named("a")])
    fields = turn.row_fields("ok")
    assert (fields["external_start"], fields["external_delay_seconds"]) == ("", None)
    turn.set_external_start("skipped", 7.5)
    fields = turn.row_fields("ok")
    assert (fields["external_start"], fields["external_delay_seconds"]) == (
        "skipped",
        7.5,
    )


# --- Choosing the side-by-side alternate -----------------------------------


def test_a_tied_answer_from_another_model_beats_the_same_model_runner_up():
    choice = pick_side_by_side_alternate(
        FRESH_REPLY,
        "model-a",
        2630.0,
        candidates=[
            ("model-a", 2542.0, THIRD_REPLY),
            ("model-b", 2210.0, SECOND_OPINION_REPLY),
        ],
        runner_up=("model-a", 2542.0, THIRD_REPLY),
    )
    assert choice is not None
    assert (choice.text, choice.model, choice.cross_model) == (
        SECOND_OPINION_REPLY,
        "model-b",
        True,
    )


def test_the_same_model_runner_up_is_the_fallback_when_no_other_model_ties():
    choice = pick_side_by_side_alternate(
        FRESH_REPLY,
        "model-a",
        2630.0,
        candidates=[
            ("model-a", 2542.0, THIRD_REPLY),
            ("model-b", 930.0, SECOND_OPINION_REPLY),
        ],
        runner_up=("model-a", 2542.0, THIRD_REPLY),
    )
    assert choice is not None
    assert (choice.text, choice.model, choice.cross_model) == (
        THIRD_REPLY,
        "model-a",
        False,
    )


def test_an_unpresentable_answer_from_another_model_is_passed_over():
    # Near-duplicate of the primary: not worth showing, whichever model.
    near_duplicate = FRESH_REPLY.replace("Great", "Good news")
    choice = pick_side_by_side_alternate(
        FRESH_REPLY,
        "model-a",
        2630.0,
        candidates=[
            ("model-b", 2600.0, near_duplicate),
            ("model-a", 2542.0, THIRD_REPLY),
        ],
        runner_up=("model-b", 2600.0, near_duplicate),
    )
    assert choice is None


def test_the_next_tied_other_model_is_tried_after_an_unpresentable_one():
    near_duplicate = FRESH_REPLY.replace("Great", "Good news")
    choice = pick_side_by_side_alternate(
        FRESH_REPLY,
        "model-a",
        2630.0,
        candidates=[
            ("model-b", 2600.0, near_duplicate),
            ("model-a", 2542.0, THIRD_REPLY),
            ("model-c", 2300.0, SECOND_OPINION_REPLY),
        ],
        runner_up=("model-b", 2600.0, near_duplicate),
    )
    assert choice is not None
    assert (choice.model, choice.cross_model) == ("model-c", True)


def test_no_alternate_when_nothing_is_closely_tied():
    choice = pick_side_by_side_alternate(
        FRESH_REPLY,
        "model-a",
        8820.0,
        candidates=[("model-b", 1900.0, SECOND_OPINION_REPLY)],
        runner_up=("model-b", 1900.0, SECOND_OPINION_REPLY),
    )
    assert choice is None


def test_an_unlabelled_winner_only_ever_gets_the_runner_up():
    choice = pick_side_by_side_alternate(
        FRESH_REPLY,
        None,
        2630.0,
        candidates=[("model-b", 2600.0, SECOND_OPINION_REPLY)],
        runner_up=("model-c", 2500.0, THIRD_REPLY),
    )
    assert choice is not None
    assert (choice.text, choice.cross_model) == (THIRD_REPLY, False)


def test_candidates_leave_out_the_winner_and_rejected_results_and_keep_fanout_order_on_ties():
    a, b, c, d, e = object(), object(), object(), object(), object()
    winner = (FRESH_REPLY, "ctx")
    completed = {
        a: winner,
        b: (THIRD_REPLY, "ctx"),
        c: (SECOND_OPINION_REPLY, "ctx"),
        d: ("rejected", "ctx"),
        e: ("", "ctx only"),
    }
    scores = {a: 10.0, b: 5.0, c: 5.0, d: float("-inf"), e: 7.0}
    labels = {a: "m-a", b: "m-b", c: "m-c", d: "m-d", e: "m-e"}
    ranked = candidates_best_first(completed, scores, labels, [a, c, b, d, e], winner)
    assert ranked == [("m-c", 5.0, SECOND_OPINION_REPLY), ("m-b", 5.0, THIRD_REPLY)]


# --- Writing the row and the pick ------------------------------------------


async def _chat():
    return await sync_to_async(OngoingChat.objects.create)(
        chat_history=[], summary_for_next_call=[]
    )


async def _offered_turn(chat, **fields):
    defaults = dict(
        outcome="ok",
        use_external=True,
        alternate_offered=True,
        alternate_model="model-b",
        winner_model="model-a",
    )
    defaults.update(fields)
    return await sync_to_async(ChatTurn.objects.create)(chat=chat, **defaults)


async def _fresh(turn):
    return await sync_to_async(ChatTurn.objects.get)(pk=turn.pk)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_turn_row_is_written_with_its_calls():
    chat = await _chat()
    turn = TurnRecord.start(True, [_Named("model-a")])
    log = CallLog(PASS_PRIMARY)
    call = log.observe(_answer(FRESH_REPLY), _Named("model-a"), "truncated")
    rejected = log.observe(_answer(THIRD_REPLY), _Named("model-a"), "full")

    def score_fn(result, task):
        return 2630.0 if task is call else float("-inf")

    await best_two_within_timelimit([call, rejected], log.scoring(score_fn), 1.0)
    turn.calls.extend(log.finish())
    turn.set_winner("model-a", 2630.0, False, None, None, False)

    assert await arecord_chat_turn(chat.id, turn, "ok") is True
    row = await sync_to_async(ChatTurn.objects.get)(pk=turn.turn_id)
    assert row.outcome == "ok"
    assert row.winner_model == "model-a"
    assert [c["status"] for c in row.calls] == ["scored", "repeat"]
    assert row.calls[1]["score"] is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_for_a_deleted_chat_is_dropped_without_raising():
    chat = await _chat()
    chat_id = chat.id
    await sync_to_async(chat.delete)()
    turn = TurnRecord.start(True, [_Named("model-a")])
    assert await arecord_chat_turn(chat_id, turn, "ok") is False
    assert await sync_to_async(ChatTurn.objects.count)() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_unknown_outcome_or_no_turn_writes_nothing():
    chat = await _chat()
    assert await arecord_chat_turn(chat.id, None, "ok") is False
    turn = TurnRecord.start(True, [_Named("model-a")])
    assert await arecord_chat_turn(chat.id, turn, "exploded") is False
    assert await sync_to_async(ChatTurn.objects.count)() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_valid_pick_is_stored_on_the_turn():
    chat = await _chat()
    turn = await _offered_turn(chat)
    assert await arecord_answer_preference(str(chat.id), str(turn.id), "alternate")
    row = await _fresh(turn)
    assert row.preferred == "alternate"
    assert row.preferred_at is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_first_pick_wins():
    chat = await _chat()
    turn = await _offered_turn(chat)
    assert await arecord_answer_preference(str(chat.id), str(turn.id), "primary")
    assert not await arecord_answer_preference(str(chat.id), str(turn.id), "alternate")
    assert (await _fresh(turn)).preferred == "primary"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_pick_for_another_chats_turn_changes_nothing():
    mine, theirs = await _chat(), await _chat()
    turn = await _offered_turn(theirs)
    assert not await arecord_answer_preference(str(mine.id), str(turn.id), "alternate")
    assert (await _fresh(turn)).preferred == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn_id, preferred",
    [
        ("not-a-uuid", "alternate"),
        (None, "alternate"),
        ({"id": 1}, "alternate"),
        ("x" * 500, "alternate"),
        ("TURN", "other"),
        ("TURN", None),
        ("TURN", ["alternate"]),
    ],
)
async def test_bad_ids_and_labels_change_nothing(turn_id, preferred):
    chat = await _chat()
    turn = await _offered_turn(chat)
    if turn_id == "TURN":
        turn_id = str(turn.id)
    assert not await arecord_answer_preference(str(chat.id), turn_id, preferred)
    assert (await _fresh(turn)).preferred == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_that_offered_no_alternate_takes_no_pick():
    chat = await _chat()
    turn = await _offered_turn(chat, alternate_offered=False, alternate_model="")
    assert not await arecord_answer_preference(str(chat.id), str(turn.id), "primary")
    assert (await _fresh(turn)).preferred == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_unknown_turn_id_changes_nothing():
    chat = await _chat()
    await _offered_turn(chat)
    assert not await arecord_answer_preference(
        str(chat.id), str(uuid.uuid4()), "primary"
    )


# --- Writing the row while a turn is cancelled -----------------------------


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._log.append((sql, params))


class _FakeConnection:
    def __init__(self, vendor):
        self.vendor = vendor
        self.executed = []

    def cursor(self):
        return _FakeCursor(self.executed)


def test_the_statement_timeout_applies_on_postgresql_only():
    postgres = _FakeConnection("postgresql")
    _bound_statements(postgres, 2000)
    assert postgres.executed == [
        ("SELECT set_config('statement_timeout', %s, true)", ["2000"])
    ]
    sqlite = _FakeConnection("sqlite")
    _bound_statements(sqlite, 2000)
    assert sqlite.executed == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_isolated_write_bounds_its_transaction_and_closes_its_connection(
    monkeypatch,
):
    chat = await _chat()
    seen = {}

    def bound(connection, ms):
        seen["in_transaction"] = connection.in_atomic_block
        seen["ms"] = ms

    closed_on = []
    monkeypatch.setattr(turn_record, "_bound_statements", bound)
    monkeypatch.setattr(
        connections, "close_all", lambda: closed_on.append(threading.get_ident())
    )
    turn = TurnRecord.start(True, [_Named("model-a")])
    turn.set_winner("model-a", 2630.0, False, None, None, False)

    assert await arecord_chat_turn_isolated(chat.id, turn, "ok") is True

    # The timeout is set inside the insert's transaction, and the thread's
    # own connection is closed afterwards, on that thread.
    assert seen == {"in_transaction": True, "ms": 2000}
    assert len(closed_on) == 1
    assert closed_on[0] != threading.get_ident()
    row = await ChatTurn.objects.aget(pk=turn.turn_id)
    assert (row.outcome, row.winner_model) == ("ok", "model-a")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stuck_isolated_write_holds_up_neither_its_caller_nor_the_chats_executor(
    monkeypatch,
):
    """A cancelled turn's write that hangs (a lock, a dead connection) must
    not hang the turn's teardown, and must not sit on the chat's
    thread-sensitive executor where the next ORM call would queue behind
    it."""
    chat = await _chat()
    entered, release = threading.Event(), threading.Event()

    def stuck(chat_id, fields):
        entered.set()
        release.wait(10)

    monkeypatch.setattr(turn_record, "_record_chat_turn_isolated_sync", stuck)
    turn = TurnRecord.start(True, [_Named("model-a")])

    # The socket's own executor, as PerConnectionThreadSensitiveMixin sets up.
    async with ThreadSensitiveContext():
        try:
            started = time.monotonic()
            written = await asyncio.wait_for(
                arecord_chat_turn_isolated(chat.id, turn, "ok", timeout=0.2), 5
            )
            waited = time.monotonic() - started
            # The next ORM call on the chat's executor runs straight away.
            count = await asyncio.wait_for(
                ChatTurn.objects.filter(chat=chat).acount(), 2
            )
            still_stuck = entered.is_set() and not release.is_set()
        finally:
            release.set()

    assert written is False
    assert waited < 2
    assert count == 0
    assert still_stuck


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_isolated_write_for_a_deleted_chat_is_dropped_without_raising():
    chat = await _chat()
    chat_id = chat.id
    await chat.adelete()
    turn = TurnRecord.start(True, [_Named("model-a")])
    assert await arecord_chat_turn_isolated(chat_id, turn, "ok") is False
    assert await arecord_chat_turn_isolated(chat_id, None, "ok") is False
    assert await ChatTurn.objects.acount() == 0

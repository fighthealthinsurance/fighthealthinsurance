"""The per-turn model-race record (chat/turn_record.py) and the choice of
side-by-side alternate (llm_client.pick_side_by_side_alternate).

A ChatTurn row says which models raced for a chat turn, how each call
ended, which model won, and which side-by-side answer the person picked. It
holds metadata only, so these tests also pin that nothing else gets in.
"""

import asyncio
import json
import uuid

import pytest
from asgiref.sync import sync_to_async

from fighthealthinsurance.chat.llm_client import (
    build_llm_calls,
    build_llm_calls_for_variants,
    build_retry_calls,
    candidates_best_first,
    pick_side_by_side_alternate,
)
from fighthealthinsurance.chat.message_preprocessor import MessageVariant
from fighthealthinsurance.chat.turn_record import (
    CALL_STATUSES,
    PASS_PRIMARY,
    PASS_RETRY,
    PREFERENCE_LABELS,
    TURN_OUTCOMES,
    CallLog,
    TurnRecord,
    arecord_answer_preference,
    arecord_chat_turn,
)
from fighthealthinsurance.ml.ml_metrics import _ANSWER_FEEDBACK_ALLOWED
from fighthealthinsurance.models import ChatTurn, OngoingChat
from fighthealthinsurance.utils import best_two_within_timelimit
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
    assert len(CALL_STATUSES) == len(set(CALL_STATUSES)) == 5


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

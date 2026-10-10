"""Chat shadow scoring: ml/chat_shadow.py (the rubric, the request and the
parse) and chat/shadow_scoring.py (the background task that stores scores
on the turn's ChatTurn row).

Chat text is patient data, so these tests pin the gates (the flag, the key
and the person's consent to outside models, each needed), the redaction
before anything leaves, that only numbers, the scorer string and an outcome
reach the row, and that every failure means "no scores".
"""

import asyncio
import datetime
import inspect
import json
import re
import threading
import time
import uuid
from unittest.mock import patch

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.contrib.auth import get_user_model
from django.test import override_settings

from fighthealthinsurance import settings as fhi_settings
from fighthealthinsurance.chat import isolated_db, shadow_scoring
from fighthealthinsurance.ml import chat_shadow, letter_quality, spend, typesafe
from fighthealthinsurance.models import (
    Appeal,
    ChatTurn,
    Denial,
    ExternalServiceHealth,
    OngoingChat,
    PatientUser,
    PriorAuthRequest,
    ProfessionalUser,
)

User = get_user_model()

ENABLED = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_CHAT_SHADOW_ENABLED=True)

MESSAGE = (
    "My name is Wilhelmina Quillfeather, email wq@example.org, phone "
    "(415) 555-0199. Will my plan cover the knee MRI?"
)
REPLY = (
    "I can't tell you whether your plan covers it, but here is how to check "
    "the plan's MRI policy and what to ask the insurer."
)
SECOND = (
    "Most plans ask for prior authorization for an MRI. Call the number on "
    "your card and ask whether the knee MRI needs one."
)
IDENTIFIERS = [("Wilhelmina Quillfeather", "PATIENT#patient")]


def _answers(
    answers=1.6, verdict=0.1, asks_again=0.05, promises=0.05, model="jev-1.13.0"
):
    return {
        "model": model,
        "answers": {
            chat_shadow.ANSWERS_QUESTION: {"type": "score", "score": answers},
            chat_shadow.ASSERTS_VERDICT: {"type": "noul", "noul": verdict},
            chat_shadow.ASKS_AGAIN: {"type": "noul", "noul": asks_again},
            chat_shadow.PROMISES_OUTCOME: {"type": "noul", "noul": promises},
        },
        "usage": {"input_tokens": 300, "output_tokens": 20},
    }


class _FakePost:
    """Stands in for chat_shadow._post: records every state sent and
    answers from a queue (a dict or an exception per reply). A pair request
    (questions given) takes one answer per reply and returns them as one
    response, each under its reply's suffix; the first reply's model names
    the response."""

    def __init__(self, *answers):
        self.answers = list(answers) or [_answers()]
        self.states = []
        self.questions = []

    def _next(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def __call__(self, state, timeout_seconds, questions=None):
        self.states.append(state)
        self.questions.append(questions)
        if questions is None:
            return self._next()
        first, second = self._next(), self._next()
        merged = {f"{k}_1": v for k, v in first["answers"].items()}
        merged.update({f"{k}_2": v for k, v in second["answers"].items()})
        return {"model": first.get("model"), "answers": merged}


def _run(coro):
    return asyncio.run(coro)


class TestSampling:
    def test_a_side_by_side_is_always_scored(self):
        with patch.object(chat_shadow, "_sample_draw", return_value=0.99):
            assert chat_shadow.wanted(True)

    def test_other_turns_are_scored_on_the_sample_rate(self):
        assert chat_shadow.sample_rate() == 0.1
        with patch.object(chat_shadow, "_sample_draw", return_value=0.09):
            assert chat_shadow.wanted(False)
        with patch.object(chat_shadow, "_sample_draw", return_value=0.1):
            assert not chat_shadow.wanted(False)
        with override_settings(TYPESAFE_CHAT_SHADOW_SAMPLE_RATE=0.0), patch.object(
            chat_shadow, "_sample_draw", return_value=0.0
        ):
            assert not chat_shadow.wanted(False)

    @pytest.mark.parametrize("value", [-0.1, 1.5, float("nan"), "half", True, None])
    def test_a_bad_rate_means_the_default(self, value):
        with override_settings(TYPESAFE_CHAT_SHADOW_SAMPLE_RATE=value):
            assert chat_shadow.sample_rate() == 0.1


# -- the rubric module ------------------------------------------------------


class TestGates:
    def test_off_under_the_test_configurations(self):
        # Base reads the flag from the environment, and django-configurations
        # copies a base class's value into every subclass. So each test
        # configuration must set it itself, or a developer's environment
        # could turn it on in a test run.
        for config in (fhi_settings.Test, fhi_settings.TestSync, fhi_settings.TestActor):
            source = inspect.getsource(config)
            assert re.search(
                r"^    TYPESAFE_CHAT_SHADOW_ENABLED = False$", source, re.M
            ), config.__name__
            assert config.TYPESAFE_CHAT_SHADOW_ENABLED is False
        assert chat_shadow.enabled() is False

    def test_needs_both_the_key_and_the_flag(self):
        with override_settings(TYPESAFE_API_KEY="k", TYPESAFE_CHAT_SHADOW_ENABLED=False):
            assert chat_shadow.enabled() is False
        with override_settings(TYPESAFE_API_KEY=None, TYPESAFE_CHAT_SHADOW_ENABLED=True):
            assert chat_shadow.enabled() is False
        with override_settings(**ENABLED):
            assert chat_shadow.enabled() is True

    def test_off_sends_nothing(self):
        fake = _FakePost()
        with patch.object(chat_shadow, "_post", fake):
            assert _run(chat_shadow.score_turn(MESSAGE, REPLY, SECOND)) is None
        assert fake.states == []

    def test_letter_ranking_on_does_not_turn_chat_scoring_on(self):
        with override_settings(
            TYPESAFE_API_KEY="k", TYPESAFE_LETTER_RANKING_ENABLED=True
        ):
            assert chat_shadow.enabled() is False


class TestState:
    def test_both_texts_are_redacted_and_labelled(self):
        state = chat_shadow.build_state(
            MESSAGE, REPLY, letter_quality.Redactor(IDENTIFIERS)
        )
        assert state.startswith(chat_shadow.MESSAGE_HEADER)
        assert chat_shadow.REPLY_HEADER in state
        for secret in ("Quillfeather", "Wilhelmina", "wq@example.org", "555-0199"):
            assert secret not in state
        assert "[PATIENT_1]" in state and "[EMAIL_1]" in state and "[PHONE_1]" in state
        assert state.endswith(REPLY)

    def test_the_message_gives_way_to_the_reply_under_the_cap(self):
        long_message = "word " * 10_000
        state = chat_shadow.build_state(
            long_message, REPLY, letter_quality.Redactor(())
        )
        assert len(state) <= chat_shadow.STATE_CHAR_CAP
        assert state.endswith(REPLY)

    def test_the_rubric_is_the_four_questions(self):
        assert set(chat_shadow.QUESTIONS) == {
            "answers_question",
            "asserts_verdict",
            "asks_again",
            "promises_outcome",
        }
        assert chat_shadow.QUESTIONS["answers_question"]["type"] == "score"
        assert len(chat_shadow.QUESTIONS["answers_question"]["criteria"]) == 3
        for noul in ("asserts_verdict", "asks_again", "promises_outcome"):
            assert chat_shadow.QUESTIONS[noul]["type"] == "noul"

    def test_the_promise_question_is_word_for_word_the_agreed_one(self):
        # The live reply gate asks this same question, and these scores are
        # the check on it, so the wording must not drift on either side.
        assert chat_shadow.QUESTIONS["promises_outcome"] == {
            "type": "noul",
            "instructions": (
                "Does THE REPLY promise or guarantee the person a result: "
                "that an appeal, claim or request will be approved, will "
                "succeed or will be won, or that something always works?"
            ),
            "criteria": {
                "true": "It promises or guarantees a result, or says one is certain.",
                "false": (
                    "It promises no result: it may say what could help, or "
                    "how such cases often go, without guaranteeing an outcome."
                ),
            },
        }


class TestParse:
    def test_a_full_answer_parses(self):
        scores = chat_shadow.parse_answers(_answers(2.0, 0.0, 1.0, 0.25))
        assert (
            scores.answers,
            scores.verdict,
            scores.asks_again,
            scores.promises,
        ) == (2.0, 0.0, 1.0, 0.25)

    @pytest.mark.parametrize(
        "broken",
        [
            _answers(answers=2.5),
            _answers(verdict=-0.1),
            _answers(asks_again=1.2),
            _answers(promises=1.01),
            _answers(promises=None),
            _answers(answers=True),
            _answers(verdict="0.5"),
            _answers(answers=float("nan")),
            {"answers": {}},
            {"model": "jev-1.13.0"},
            None,
        ],
    )
    def test_anything_unexpected_is_refused(self, broken):
        with pytest.raises(chat_shadow.ShadowScoringError):
            chat_shadow.parse_answers(broken)

    def test_an_answer_without_the_promise_question_is_refused(self):
        # What a rubric 1 answer looks like: three questions, no promises.
        payload = _answers()
        del payload["answers"][chat_shadow.PROMISES_OUTCOME]
        with pytest.raises(chat_shadow.ShadowScoringError):
            chat_shadow.parse_answers(payload)


class TestComposite:
    def test_higher_is_better_and_yes_answers_count_against(self):
        best = chat_shadow.composite_score(2.0, 0.0, 0.0, 0.0)
        worst = chat_shadow.composite_score(0.0, 1.0, 1.0, 1.0)
        assert best == pytest.approx(1.0)
        assert worst == pytest.approx(0.0)
        assert chat_shadow.composite_score(1.0, 0.5, 0.5, 0.5) == pytest.approx(0.5)
        assert chat_shadow.composite_score(2.0, 1.0, 0.0, 0.0) < best

    def test_the_four_parts_weigh_the_same(self):
        assert chat_shadow.composite_score(1.6, 0.1, 0.05, 0.3) == pytest.approx(
            (0.8 + 0.9 + 0.95 + 0.7) / 4
        )

    def test_a_promised_result_lowers_the_composite(self):
        clean = chat_shadow.composite_score(2.0, 0.0, 0.0, 0.0)
        promised = chat_shadow.composite_score(2.0, 0.0, 0.0, 1.0)
        assert promised == pytest.approx(0.75)
        assert promised < clean
        assert chat_shadow.ReplyScores(2.0, 0.0, 0.0, 1.0).composite == promised

    def test_a_missing_part_means_no_composite(self):
        assert chat_shadow.composite_score(None, 0.0, 0.0, 0.0) is None
        assert chat_shadow.composite_score(1.0, True, 0.0, 0.0) is None
        assert chat_shadow.composite_score(1.0, 0.0, float("inf"), 0.0) is None
        # A row scored before the promise question existed has no promises.
        assert chat_shadow.composite_score(2.0, 0.0, 0.0, None) is None
        assert chat_shadow.composite_score(2.0, 0.0, 0.0, float("nan")) is None


class TestScorer:
    def test_scorer_names_the_answering_model_and_the_chat_rubric(self):
        assert chat_shadow.scorer_for(_answers(model="jev-1.14.0")) == (
            "typesafe/jev-1.14.0/chat-rubric-3"
        )
        assert chat_shadow.SCORER == "typesafe/jev-1.13.0/chat-rubric-3"

    def test_only_the_current_chat_rubric_counts(self):
        assert chat_shadow.same_rubric("typesafe/jev-1.13.0/chat-rubric-3")
        # Rubric 1 had no promise question, and rubric 2 asked about each
        # reply in its own request: their rows are never averaged in.
        assert not chat_shadow.same_rubric("typesafe/jev-1.13.0/chat-rubric-1")
        assert not chat_shadow.same_rubric("typesafe/jev-1.13.0/chat-rubric-2")
        # A letter score is a different rubric altogether.
        assert not chat_shadow.same_rubric(letter_quality.SCORER)
        assert not chat_shadow.same_rubric("")


class TestScoreTurn:
    def test_both_replies_go_in_one_request_and_are_redacted(self):
        fake = _FakePost(_answers(1.8, 0.1, 0.0, 0.05), _answers(0.9, 0.7, 0.2, 0.6))
        with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
            result = _run(
                chat_shadow.score_turn(MESSAGE, REPLY, SECOND, identifiers=IDENTIFIERS)
            )
        assert result.outcome == chat_shadow.SCORED
        assert result.scorer == "typesafe/jev-1.13.0/chat-rubric-3"
        assert result.winner == chat_shadow.ReplyScores(1.8, 0.1, 0.0, 0.05)
        assert result.second == chat_shadow.ReplyScores(0.9, 0.7, 0.2, 0.6)
        (state,) = fake.states
        assert state.index("THE REPLY 1:") < state.index(REPLY)
        assert state.index("THE REPLY 2:") < state.index(SECOND)
        assert state.endswith(SECOND)
        assert "Quillfeather" not in state and "wq@example.org" not in state
        (questions,) = fake.questions
        assert set(questions) == {
            f"{name}_{k}" for name in chat_shadow.QUESTIONS for k in (1, 2)
        }
        assert "THE REPLY 2" in questions[f"{chat_shadow.ASKS_AGAIN}_2"]["instructions"]

    def test_the_request_goes_through_the_typesafe_client(self):
        seen = {}

        async def fake_ask(state, questions, *, timeout_seconds, use, task="other"):
            seen["questions"] = questions
            seen["timeout"] = timeout_seconds
            seen["use"] = use
            return _answers()

        with override_settings(**ENABLED), patch.object(typesafe, "ask", fake_ask):
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY))
        assert result.outcome == chat_shadow.SCORED
        assert seen["questions"] is chat_shadow.QUESTIONS
        assert seen["timeout"] == 20
        # Counted against TypeSafe's chat budget.
        assert seen["use"] == spend.CHAT

    def test_no_second_answer_means_one_request(self):
        fake = _FakePost()
        with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY, None))
        assert len(fake.states) == 1
        assert result.second is None and result.winner is not None

    def test_nothing_to_score_or_over_the_raw_bound_sends_nothing(self):
        fake = _FakePost()
        huge = "x" * (chat_shadow.RAW_CHAR_BOUND + 1)
        with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
            assert _run(chat_shadow.score_turn("", REPLY)) is None
            assert _run(chat_shadow.score_turn(MESSAGE, "   ")) is None
            assert _run(chat_shadow.score_turn(huge, REPLY)) is None
            # An over-long second answer is dropped; the reply is still scored.
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY, huge))
        assert len(fake.states) == 1
        assert result.second is None

    @pytest.mark.parametrize(
        "error, summary",
        [
            (typesafe.TypeSafeError("HTTP 429", status=429), "HTTP 429"),
            (typesafe.TypeSafeError("HTTP 529", status=529), "HTTP 529"),
            (ConnectionResetError("reset by peer"), "ConnectionResetError"),
        ],
    )
    def test_an_error_fails_closed_with_no_scores(self, error, summary):
        fake = _FakePost(_answers(), error)
        with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY, SECOND))
        assert result.outcome == chat_shadow.FAILED
        assert result.failure == summary
        # All or nothing: the reply that did get an answer keeps no score.
        assert (result.winner, result.second, result.scorer) == (None, None, "")

    def test_a_malformed_answer_fails_closed(self):
        fake = _FakePost(_answers(answers=7.0))
        with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY))
        assert result.outcome == chat_shadow.FAILED
        assert result.failure == "ShadowScoringError"
        assert result.winner is None

    def test_a_pair_missing_one_replys_answer_fails_closed(self):
        async def half(state, timeout_seconds, questions=None):
            payload = _answers()
            return {
                "model": "jev-1.13.0",
                "answers": {f"{k}_1": v for k, v in payload["answers"].items()},
            }

        with override_settings(**ENABLED), patch.object(chat_shadow, "_post", half):
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY, SECOND))
        assert result.outcome == chat_shadow.FAILED
        assert result.winner is None and result.second is None

    def test_a_slow_answer_times_out(self):
        async def slow(state, timeout_seconds):
            await asyncio.sleep(5)
            return _answers()

        with (
            override_settings(**ENABLED),
            patch.object(chat_shadow, "_post", slow),
            patch.object(chat_shadow, "GRACE_SECONDS", 0.0),
        ):
            result = _run(
                chat_shadow.score_turn(MESSAGE, REPLY, timeout_seconds=0.05)
            )
        assert result.outcome == chat_shadow.TIMEOUT
        assert result.failure == "timeout"
        assert result.winner is None

    def test_a_failure_logs_no_text(self):
        seen = []
        error = RuntimeError(f"upstream echoed: {MESSAGE}")
        fake = _FakePost(error)
        with (
            override_settings(**ENABLED),
            patch.object(chat_shadow, "_post", fake),
            patch.object(
                chat_shadow.logger,
                "warning",
                side_effect=lambda m, *a, **k: seen.append(str(m)),
            ),
        ):
            result = _run(chat_shadow.score_turn(MESSAGE, REPLY))
        assert result.outcome == chat_shadow.FAILED
        assert seen
        for line in seen:
            assert "Quillfeather" not in line and "knee" not in line
        assert "Quillfeather" not in result.failure


# -- starting the background task ------------------------------------------


def _start(**overrides):
    kwargs = dict(
        chat_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        external_allowed=True,
        message=MESSAGE,
        reply=REPLY,
        second=SECOND,
    )
    kwargs.update(overrides)
    return shadow_scoring.start(**kwargs)


class TestStartGates:
    """Every gate on its own stops the request before anything is sent."""

    @pytest.mark.parametrize(
        "settings_, consent",
        [
            (dict(TYPESAFE_API_KEY="k", TYPESAFE_CHAT_SHADOW_ENABLED=False), True),
            (dict(TYPESAFE_API_KEY=None, TYPESAFE_CHAT_SHADOW_ENABLED=True), True),
            (ENABLED, False),
        ],
        ids=["flag off", "no key", "consent off"],
    )
    def test_a_closed_gate_starts_nothing(self, settings_, consent):
        fake = _FakePost()

        async def go():
            task = _start(external_allowed=consent)
            await asyncio.sleep(0)
            return task

        with override_settings(**settings_), patch.object(chat_shadow, "_post", fake):
            assert _run(go()) is None
        assert fake.states == []

    def test_nothing_to_score_starts_nothing(self):
        async def go():
            return _start(reply="  ")

        with override_settings(**ENABLED):
            assert _run(go()) is None

    def test_a_full_slot_table_starts_nothing(self):
        async def go():
            with patch.object(shadow_scoring, "MAX_IN_FLIGHT", 0):
                return _start()

        with override_settings(**ENABLED):
            assert _run(go()) is None


# -- the task against the database -----------------------------------------


async def _chat_and_turn(**chat_fields):
    chat = await OngoingChat.objects.acreate(**chat_fields)
    turn = await ChatTurn.objects.acreate(
        chat=chat,
        outcome="ok",
        use_external=True,
        winner_model="model-a",
        runner_up_model="model-b",
    )
    return chat, turn


def _row_blob(row):
    return json.dumps(
        {f.name: getattr(row, f.attname) for f in ChatTurn._meta.concrete_fields},
        default=str,
        ensure_ascii=False,
    )


async def _run_task(chat_id, turn_id, fake, second=SECOND):
    with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
        task = shadow_scoring.start(
            chat_id=chat_id,
            turn_id=turn_id,
            external_allowed=True,
            message=MESSAGE,
            reply=REPLY,
            second=second,
        )
        assert task is not None
        await asyncio.wait_for(task, 10)


async def _health(service):
    return await ExternalServiceHealth.objects.filter(service=service).afirst()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_scores_land_on_the_row_as_numbers_only():
    chat, turn = await _chat_and_turn()
    fake = _FakePost(_answers(1.8, 0.1, 0.0, 0.05), _answers(0.9, 0.7, 0.2, 0.6))
    await _run_task(chat.id, turn.id, fake)

    row = await ChatTurn.objects.aget(pk=turn.pk)
    assert row.shadow_outcome == "scored"
    assert row.shadow_scorer == "typesafe/jev-1.13.0/chat-rubric-3"
    assert (
        row.shadow_winner_answers,
        row.shadow_winner_verdict,
        row.shadow_winner_asks_again,
        row.shadow_winner_promises,
    ) == (1.8, 0.1, 0.0, 0.05)
    assert (
        row.shadow_second_answers,
        row.shadow_second_verdict,
        row.shadow_second_asks_again,
        row.shadow_second_promises,
    ) == (0.9, 0.7, 0.2, 0.6)
    blob = _row_blob(row)
    for text in ("Quillfeather", "knee MRI", REPLY[:30], SECOND[:30], "PATIENT_1"):
        assert text not in blob, f"{text!r} stored on the turn row"
    health = await _health(chat_shadow.SERVICE)
    assert health is not None and health.last_success_at is not None
    # The letter scorer's own record is untouched.
    assert await _health(letter_quality.SERVICE) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_job_that_finds_the_database_threads_full_sends_nothing(
    monkeypatch,
):
    """Jobs admitted together can reach their database steps after the
    threads have filled up, since admission counted them before any had
    started. The step then starts no thread of its own, and with no
    identifier lookup nothing is sent."""
    chat, turn = await _chat_and_turn()
    # Admission saw free slots; by the first step they are all taken.
    monkeypatch.setattr(isolated_db, "running", lambda name: 0)
    monkeypatch.setattr(
        isolated_db,
        "_running",
        {shadow_scoring.DB_THREAD_NAME: shadow_scoring.DB_THREAD_LIMIT},
    )
    fake = _FakePost()
    await _run_task(chat.id, turn.id, fake)

    assert fake.states == []
    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == ""
    assert isolated_db._running == {
        shadow_scoring.DB_THREAD_NAME: shadow_scoring.DB_THREAD_LIMIT
    }


class _HeldPost(_FakePost):
    """A _FakePost whose requests wait until released, counting each one
    as it is sent."""

    def __init__(self, *answers):
        super().__init__(*answers)
        self.sent = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, state, timeout_seconds, questions=None):
        self.sent += 1
        self.entered.set()
        await self.release.wait()
        return await super().__call__(state, timeout_seconds, questions)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_job_that_sends_keeps_a_place_to_store_the_answer(monkeypatch):
    """Two places short of the thread limit, a job that has sent its texts
    keeps the place for its score write while it waits: a job started
    meanwhile finds no place, sends nothing, and cannot take it."""
    chat, turn = await _chat_and_turn()
    other_chat, other_turn = await _chat_and_turn()
    # Threads timed-out steps left behind.
    monkeypatch.setitem(
        isolated_db._running,
        shadow_scoring.DB_THREAD_NAME,
        shadow_scoring.DB_THREAD_LIMIT - 2,
    )
    held = _HeldPost()
    with override_settings(**ENABLED), patch.object(chat_shadow, "_post", held):
        first = shadow_scoring.start(
            chat_id=chat.id,
            turn_id=turn.id,
            external_allowed=True,
            message=MESSAGE,
            reply=REPLY,
            second=SECOND,
        )
        assert first is not None
        await asyncio.wait_for(held.entered.wait(), 5)
        sent_by_first = held.sent
        later = shadow_scoring.start(
            chat_id=other_chat.id,
            turn_id=other_turn.id,
            external_allowed=True,
            message=MESSAGE,
            reply=REPLY,
            second=SECOND,
        )
        assert later is not None
        await asyncio.wait_for(later, 10)
        assert held.sent == sent_by_first
        held.release.set()
        await asyncio.wait_for(first, 10)

    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == "scored"
    assert (await ChatTurn.objects.aget(pk=other_turn.pk)).shadow_outcome == ""
    assert (
        isolated_db.running(shadow_scoring.DB_THREAD_NAME)
        == shadow_scoring.DB_THREAD_LIMIT - 2
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_full_set_of_jobs_started_together_all_send():
    """Each job holds two thread places during its lookup, so the thread
    limit is two per job: MAX_IN_FLIGHT jobs admitted at once all get to
    send, none is dropped for want of a place."""
    turns = [await _chat_and_turn() for _ in range(shadow_scoring.MAX_IN_FLIGHT)]
    held = _HeldPost()
    with override_settings(**ENABLED), patch.object(chat_shadow, "_post", held):
        tasks = [
            shadow_scoring.start(
                chat_id=chat.id,
                turn_id=turn.id,
                external_allowed=True,
                message=MESSAGE,
                reply=REPLY,
                second=SECOND,
            )
            for chat, turn in turns
        ]
        assert all(task is not None for task in tasks)
        for _ in range(200):
            if held.sent == len(tasks):
                break
            await asyncio.sleep(0.05)
        assert held.sent == len(tasks)
        held.release.set()
        # Eight score writes at the same instant can hit sqlite's single
        # writer lock in tests; PostgreSQL takes them. What matters here is
        # that every job got a place and sent.
        await asyncio.wait_for(asyncio.gather(*tasks), 20)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_failure_stores_the_outcome_and_no_scores():
    chat, turn = await _chat_and_turn()
    fake = _FakePost(typesafe.TypeSafeError("HTTP 529", status=529))
    await _run_task(chat.id, turn.id, fake)

    row = await ChatTurn.objects.aget(pk=turn.pk)
    assert row.shadow_outcome == "failed"
    assert row.shadow_scorer == ""
    assert row.shadow_winner_answers is None and row.shadow_second_answers is None
    assert row.shadow_winner_promises is None and row.shadow_second_promises is None
    health = await _health(chat_shadow.SERVICE)
    assert health.last_failure == "HTTP 529" and health.last_success_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_linked_accounts_are_redacted_before_sending():
    user = await User.objects.acreate_user(
        username="zephyrine", password="pw", email="zeph@example.com",
        first_name="Zephyrine", last_name="Oakhollow",
    )
    professional = await ProfessionalUser.objects.acreate(
        user=user, active=True, npi_number="1234567893"
    )
    chat, turn = await _chat_and_turn(professional_user=professional)
    fake = _FakePost()
    message = "Dr Zephyrine Oakhollow here, NPI 1234567893, zeph@example.com"
    with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
        task = shadow_scoring.start(
            chat_id=chat.id,
            turn_id=turn.id,
            external_allowed=True,
            message=message,
            reply="Thanks, Zephyrine. " + REPLY,
        )
        await asyncio.wait_for(task, 10)
    (state,) = fake.states
    for secret in ("Zephyrine", "Oakhollow", "1234567893", "zeph@example.com"):
        assert secret not in state, secret


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_deleted_chat_sends_nothing():
    chat, turn = await _chat_and_turn()
    chat_id = chat.id
    await sync_to_async(chat.delete)()
    fake = _FakePost()
    await _run_task(chat_id, turn.id, fake)
    assert fake.states == []
    assert await _health(chat_shadow.SERVICE) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_is_scored_once():
    chat, turn = await _chat_and_turn()
    await _run_task(chat.id, turn.id, _FakePost(_answers(2.0, 0.0, 0.0)))
    await _run_task(chat.id, turn.id, _FakePost(_answers(0.0, 1.0, 1.0)))
    row = await ChatTurn.objects.aget(pk=turn.pk)
    assert row.shadow_winner_answers == 2.0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_scores_only_land_on_a_turn_of_the_same_chat():
    chat, turn = await _chat_and_turn()
    other, _ = await _chat_and_turn()
    await _run_task(other.id, turn.id, _FakePost())
    row = await ChatTurn.objects.aget(pk=turn.pk)
    assert row.shadow_outcome == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_start_returns_at_once_while_the_request_is_still_running():
    chat, turn = await _chat_and_turn()
    release, started = asyncio.Event(), asyncio.Event()

    async def blocked(state, timeout_seconds):
        started.set()
        await release.wait()
        return _answers()

    with override_settings(**ENABLED), patch.object(chat_shadow, "_post", blocked):
        task = shadow_scoring.start(
            chat_id=chat.id,
            turn_id=turn.id,
            external_allowed=True,
            message=MESSAGE,
            reply=REPLY,
        )
        # start() came back before the request even began.
        assert task is not None and not started.is_set()
        assert shadow_scoring.in_flight() == 1
        await asyncio.wait_for(started.wait(), 5)
        assert not task.done()
        assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == ""
        release.set()
        await asyncio.wait_for(task, 5)
    assert shadow_scoring.in_flight() == 0
    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == "scored"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_linked_appeal_and_prior_auth_are_redacted_before_sending():
    doctor_user = await User.objects.acreate_user(
        username="drplover", password="pw", email="plover@clinic.example",
        first_name="Charadrius", last_name="Plover",
    )
    doctor = await ProfessionalUser.objects.acreate(user=doctor_user, active=True)
    patient_user = await User.objects.acreate_user(
        username="rosalind", password="pw", email="rosalind@example.org",
        first_name="Rosalind", last_name="Featherstone",
    )
    patient = await PatientUser.objects.acreate(user=patient_user)
    chat, turn = await _chat_and_turn(professional_user=doctor)
    denial = await Denial.objects.acreate(
        hashed_email="h",
        denial_text="denied",
        claim_id="CLM-5521907",
        plan_id="PLN-EAST-88",
        patient_user=patient,
    )
    await Appeal.objects.acreate(
        hashed_email="h", chat=chat, for_denial=denial, patient_user=patient
    )
    await PriorAuthRequest.objects.acreate(
        chat=chat,
        diagnosis="d",
        treatment="t",
        insurance_company="i",
        patient_name="Barnaby Wickersham",
        member_id="MBR-3310-2207",
        patient_dob=datetime.date(1979, 11, 23),
    )
    message = (
        "Rosalind Featherstone's claim CLM-5521907 on plan PLN-EAST-88 was "
        "denied; Barnaby Wickersham, member MBR-3310-2207, born 11/23/1979, "
        "needs a prior auth too."
    )
    fake = _FakePost()
    with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
        task = shadow_scoring.start(
            chat_id=chat.id,
            turn_id=turn.id,
            external_allowed=True,
            message=message,
            reply="Thanks, Rosalind. For CLM-5521907, " + REPLY,
        )
        await asyncio.wait_for(task, 10)
    (state,) = fake.states
    for secret in (
        "Rosalind",
        "Featherstone",
        "CLM-5521907",
        "PLN-EAST-88",
        "Barnaby",
        "Wickersham",
        "MBR-3310-2207",
        "11/23/1979",
    ):
        assert secret not in state, secret
    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == "scored"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_failed_lookup_of_a_linked_record_sends_nothing():
    chat, turn = await _chat_and_turn()
    denial = await Denial.objects.acreate(
        hashed_email="h", denial_text="denied", claim_id="CLM-1"
    )
    await Appeal.objects.acreate(hashed_email="h", chat=chat, for_denial=denial)
    fake = _FakePost()
    with patch(
        "fighthealthinsurance.common_view_logic.scoring_redactions",
        side_effect=RuntimeError("db away"),
    ):
        await _run_task(chat.id, turn.id, fake)
    assert fake.states == []
    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == ""
    assert await _health(chat_shadow.SERVICE) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_failed_health_note_still_stores_the_scores(monkeypatch):
    chat, turn = await _chat_and_turn()

    def broken(*args, **kwargs):
        raise RuntimeError("health row away")

    monkeypatch.setattr(ExternalServiceHealth, "_advance_sync", broken)
    await _run_task(chat.id, turn.id, _FakePost(_answers(1.8, 0.1, 0.0)))
    row = await ChatTurn.objects.aget(pk=turn.pk)
    assert row.shadow_outcome == "scored"
    assert row.shadow_winner_answers == 1.8


# -- bounds, and staying off the chat's executor -----------------------------


def test_the_job_bound_covers_every_step():
    assert shadow_scoring.job_seconds(20.0) == (
        2 * shadow_scoring.DB_STEP_SECONDS
        + 20.0
        + chat_shadow.GRACE_SECONDS
        + shadow_scoring.JOB_SPARE_SECONDS
    )


def _stuck_step(monkeypatch, name, returns):
    """Replace one database step with one that blocks until released."""
    entered, release = threading.Event(), threading.Event()

    def stuck(*args):
        entered.set()
        release.wait(10)
        return returns

    monkeypatch.setattr(shadow_scoring, name, stuck)
    monkeypatch.setattr(shadow_scoring, "DB_STEP_SECONDS", 0.2)
    return entered, release


async def _chat_next_orm_call_while_stuck(chat, turn, fake, entered, release):
    """Start the job on the chat's own executor, wait until its step is
    stuck, then time the chat's next ORM call on that executor."""
    # The socket's own executor, as PerConnectionThreadSensitiveMixin sets up.
    async with ThreadSensitiveContext():
        try:
            with override_settings(**ENABLED), patch.object(chat_shadow, "_post", fake):
                task = shadow_scoring.start(
                    chat_id=chat.id,
                    turn_id=turn.id,
                    external_allowed=True,
                    message=MESSAGE,
                    reply=REPLY,
                )
                assert task is not None
                assert await asyncio.to_thread(entered.wait, 5)
                started = time.monotonic()
                count = await asyncio.wait_for(
                    ChatTurn.objects.filter(chat=chat).acount(), 2
                )
                waited = time.monotonic() - started
                await asyncio.wait_for(task, 5)
            still_stuck = not release.is_set()
        finally:
            release.set()
    return count, waited, still_stuck


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stuck_identifier_lookup_holds_up_neither_the_job_nor_the_chat(
    monkeypatch,
):
    chat, turn = await _chat_and_turn()
    entered, release = _stuck_step(monkeypatch, "chat_redactions", [])
    fake = _FakePost()

    count, waited, still_stuck = await _chat_next_orm_call_while_stuck(
        chat, turn, fake, entered, release
    )

    assert count == 1 and waited < 1
    assert still_stuck
    # No identifiers in time: nothing was sent, and the slot is free again.
    assert fake.states == []
    assert shadow_scoring.in_flight() == 0
    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stuck_score_write_holds_up_neither_the_job_nor_the_chat(
    monkeypatch,
):
    chat, turn = await _chat_and_turn()
    entered, release = _stuck_step(monkeypatch, "_record_result_sync", 0)
    # The lookup before the write makes no query here: on the shared test
    # database a real one can wait on a lock another test's thread still
    # holds, time out, and end the job before it reaches the write.
    monkeypatch.setattr(shadow_scoring, "chat_redactions", lambda chat_id: [])
    fake = _FakePost()

    count, waited, still_stuck = await _chat_next_orm_call_while_stuck(
        chat, turn, fake, entered, release
    )

    assert count == 1 and waited < 1
    assert still_stuck
    assert len(fake.states) == 1
    assert shadow_scoring.in_flight() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_whole_job_stops_at_its_bound(monkeypatch):
    chat, turn = await _chat_and_turn()

    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_shadow, "score_turn", never)
    monkeypatch.setattr(shadow_scoring, "job_seconds", lambda request_seconds: 0.3)
    with override_settings(**ENABLED):
        task = shadow_scoring.start(
            chat_id=chat.id,
            turn_id=turn.id,
            external_allowed=True,
            message=MESSAGE,
            reply=REPLY,
        )
        started = time.monotonic()
        await asyncio.wait_for(task, 5)
    assert time.monotonic() - started < 2
    assert shadow_scoring.in_flight() == 0
    assert (await ChatTurn.objects.aget(pk=turn.pk)).shadow_outcome == ""


def test_lingering_database_threads_fill_the_slots_too(monkeypatch):
    monkeypatch.setattr(
        isolated_db,
        "running",
        lambda name: shadow_scoring.DB_THREAD_LIMIT
        if name == shadow_scoring.DB_THREAD_NAME
        else 0,
    )

    async def go():
        return _start()

    with override_settings(**ENABLED):
        assert _run(go()) is None

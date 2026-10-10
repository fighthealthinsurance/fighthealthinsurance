"""Provider transport timeouts clamped to the attempt budget (enable gate 3).

The appeal task timeout defaults to 300s while the journey's generation
budget is 240s, so one provider call could outlive the whole attempt and keep
spending after Temporal moved on -- three attempts of that per workflow.
start_to_close does not kill the thread holding the socket, so the call must
not be allowed to outlive the budget in the first place.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async

from fighthealthinsurance.ml import ml_models


def test_unclamped_outside_an_attempt():
    """Interactive and chat paths must be untouched."""
    assert ml_models.remaining_attempt_budget() is None
    assert ml_models.ml_task_timeout("appeal") == 300.0
    assert ml_models.ml_task_timeout("chat") == 90.0


def test_clamped_to_whats_left_of_the_budget():
    with ml_models.attempt_deadline(30.0):
        t = ml_models.ml_task_timeout("appeal")
    # 300s configured, 30s left -> 30s.
    assert 29.0 < t <= 30.0, t


def test_configured_value_wins_when_it_is_the_tighter_bound():
    """The clamp is a ceiling, never a floor: a short task timeout is not
    stretched to fill the budget."""
    with ml_models.attempt_deadline(600.0):
        assert ml_models.ml_task_timeout("entity") == 45.0


def test_never_returns_a_zero_or_negative_timeout():
    """requests treats 0 as fail-immediately and None as wait-forever, so a
    budget that has already run out must still yield a small positive value
    rather than either of those."""
    with ml_models.attempt_deadline(0.0):
        t = ml_models.ml_task_timeout("appeal")
    assert t == ml_models.MIN_TASK_TIMEOUT_SECONDS
    assert t > 0


def test_the_floor_never_lengthens_a_short_configured_timeout(monkeypatch):
    """The floor guards against handing a client 0 or a negative value; it is
    not a licence to make a deliberately short timeout longer. max(1.0, ...)
    turned a configured 0.5s into 1.0s (external review)."""
    monkeypatch.setenv("FHI_ML_TIMEOUT_ENTITY", "0.5")
    with ml_models.attempt_deadline(0.0):
        t = ml_models.ml_task_timeout("entity")
    assert t == 0.5, t
    assert t > 0


def test_nested_blocks_keep_the_tighter_deadline():
    """An inner stage may not award itself more time than the attempt has."""
    with ml_models.attempt_deadline(20.0):
        with ml_models.attempt_deadline(600.0):
            assert ml_models.ml_task_timeout("appeal") <= 20.0
        assert ml_models.ml_task_timeout("appeal") <= 20.0
    assert ml_models.remaining_attempt_budget() is None


def test_the_deadline_is_restored_even_when_the_block_raises():
    with pytest.raises(RuntimeError):
        with ml_models.attempt_deadline(10.0):
            raise RuntimeError("boom")
    assert ml_models.remaining_attempt_budget() is None


@pytest.mark.asyncio
async def test_the_clamp_reaches_a_threaded_provider_call():
    """The load-bearing assumption: provider calls run in worker THREADS via
    asgiref, and a ContextVar set on the event loop must reach them. If
    asgiref did not copy the context, the clamp would silently do nothing
    where it matters most."""

    def in_a_worker_thread() -> float:
        # Exactly what a blocking backend does before opening its socket.
        return ml_models.ml_task_timeout("appeal")

    with ml_models.attempt_deadline(25.0):
        seen = await sync_to_async(in_a_worker_thread, thread_sensitive=False)()
    assert 24.0 < seen <= 25.0, seen


@pytest.mark.asyncio
async def test_concurrent_attempts_do_not_see_each_others_budget():
    """A ContextVar, not a global: two generations on one worker must not
    clamp each other."""

    async def run(budget: float, hold: float) -> float:
        with ml_models.attempt_deadline(budget):
            await asyncio.sleep(hold)
            return ml_models.ml_task_timeout("appeal")

    tight, loose = await asyncio.gather(run(5.0, 0.05), run(120.0, 0.05))
    assert tight <= 5.0, tight
    assert 100.0 < loose <= 120.0, loose


# The appeal path's calls (_checked_infer) under a requester deadline: the WS
# path sets no attempt_deadline, only ``deadline``, and the retry after a
# timeout used to get a fresh 300s whatever was left of it.
_GOOD_DRAFT = "The denied service is medically necessary for this patient."
_CHECKED_INFER_KWARGS = dict(
    prompt="denial text",
    patient_context=None,
    plan_context=None,
    infer_type="medically_necessary",
    pubmed_context=None,
    system_prompt="sys",
    temperature=0.5,
)


def _model_answering(*answers):
    """An appeal backend whose calls return ``answers`` in turn."""
    model = ml_models.RemoteFullOpenLike("http://deadline.example/v1", "tok", "m")
    model._infer_no_context = AsyncMock(side_effect=list(answers))  # type: ignore[method-assign]
    return model


class _Clock:
    """ml_models' view of time, moved on by hand, so a call can take ten
    seconds without the test waiting them out."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock():
    """Patch ml_models' clock only: the event loop keeps the real one."""
    moved = _Clock()
    with patch.object(ml_models, "time", moved):
        yield moved


# Under MIN_RETRY_WINDOW_SECONDS of this is left once the first call has
# taken _FIRST_CALL_SECONDS: time to ask once, none for the retry.
_DEADLINE_SECONDS = 20.0
_FIRST_CALL_SECONDS = 10.0


def _model_with_a_slow_first_call(clock, *answers):
    """_model_answering, but the first call takes _FIRST_CALL_SECONDS on
    ``clock``. An exception among ``answers`` is raised by its call."""
    model = _model_answering()
    pending = list(answers)

    async def answer(**kwargs):
        if len(pending) == len(answers):
            clock.offset += _FIRST_CALL_SECONDS
        given = pending.pop(0)
        if isinstance(given, BaseException):
            raise given
        return given

    model._infer_no_context = AsyncMock(side_effect=answer)  # type: ignore[method-assign]
    return model


@pytest.mark.asyncio
async def test_a_call_never_outlives_the_requesters_deadline():
    model = _model_answering(_GOOD_DRAFT)
    await model._checked_infer(
        **_CHECKED_INFER_KWARGS, deadline=time.monotonic() + 60.0
    )
    assert model._infer_no_context.await_args.kwargs["timeout"] <= 60.0


@pytest.mark.asyncio
async def test_the_retry_gets_only_what_is_left_of_the_deadline():
    model = _model_answering(None, _GOOD_DRAFT)
    await model._checked_infer(
        **_CHECKED_INFER_KWARGS, deadline=time.monotonic() + 60.0
    )
    assert model._infer_no_context.await_args_list[1].kwargs["timeout"] <= 60.0


@pytest.mark.asyncio
async def test_the_retry_is_skipped_when_too_little_of_the_deadline_is_left(clock):
    """A letter does not fit in a few seconds: the retry would only spend a
    request (a paid one, on a hosted model) nobody reads."""
    model = _model_with_a_slow_first_call(clock, None, _GOOD_DRAFT)
    result = await model._checked_infer(
        **_CHECKED_INFER_KWARGS, deadline=clock.monotonic() + _DEADLINE_SECONDS
    )
    assert (result, model._infer_no_context.await_count) == ([], 1)


@pytest.mark.asyncio
async def test_the_retry_is_skipped_when_too_little_of_the_attempt_is_left(clock):
    model = _model_with_a_slow_first_call(clock, None, _GOOD_DRAFT)
    with ml_models.attempt_deadline(_DEADLINE_SECONDS):
        await model._checked_infer(**_CHECKED_INFER_KWARGS)
    assert model._infer_no_context.await_count == 1


@pytest.mark.asyncio
async def test_the_first_call_is_not_sent_with_too_little_of_the_deadline_left():
    """It could only time out: a paid request nobody reads, filed as an
    outage of a healthy model."""
    model = _model_answering(_GOOD_DRAFT)
    with pytest.raises(ml_models.DeadlineSkipped):
        await model._checked_infer(
            **_CHECKED_INFER_KWARGS,
            deadline=time.monotonic() + ml_models.MIN_RETRY_WINDOW_SECONDS - 5.0,
        )
    assert model._infer_no_context.await_count == 0


@pytest.mark.asyncio
async def test_the_first_call_is_not_sent_with_too_little_of_the_attempt_left():
    model = _model_answering(_GOOD_DRAFT)
    with ml_models.attempt_deadline(ml_models.MIN_RETRY_WINDOW_SECONDS - 5.0):
        with pytest.raises(ml_models.DeadlineSkipped):
            await model._checked_infer(**_CHECKED_INFER_KWARGS)
    assert model._infer_no_context.await_count == 0


@pytest.mark.asyncio
async def test_the_retry_runs_while_there_is_time_for_it():
    model = _model_answering(None, _GOOD_DRAFT)
    await model._checked_infer(
        **_CHECKED_INFER_KWARGS, deadline=time.monotonic() + 200.0
    )
    assert model._infer_no_context.await_count == 2


@pytest.mark.asyncio
async def test_a_model_never_asked_raises_deadline_skipped():
    """An empty result read as a model that answered nothing; the caller
    could only guess from the clock that it was never asked."""
    model = _model_answering(_GOOD_DRAFT)
    with pytest.raises(ml_models.DeadlineSkipped):
        await model._checked_infer(
            **_CHECKED_INFER_KWARGS, deadline=time.monotonic() - 1.0
        )


@pytest.mark.asyncio
async def test_a_deadline_skip_is_recorded_once():
    model = _model_answering(_GOOD_DRAFT)
    with patch.object(ml_models, "record_ml_result") as recorded:
        with pytest.raises(ml_models.DeadlineSkipped):
            await model._checked_infer(
                **_CHECKED_INFER_KWARGS, deadline=time.monotonic() - 1.0
            )
    assert [c.args[2] for c in recorded.call_args_list] == ["skipped_deadline"]


@pytest.mark.asyncio
async def test_a_skipped_retry_keeps_what_the_asked_call_gave(clock):
    """The model was asked and answered nothing: that is no_completion, and
    no DeadlineSkipped, though no time was left for the retry."""
    model = _model_with_a_slow_first_call(clock, None, _GOOD_DRAFT)
    with patch.object(ml_models, "record_ml_result") as recorded:
        await model._checked_infer(
            **_CHECKED_INFER_KWARGS, deadline=clock.monotonic() + _DEADLINE_SECONDS
        )
    assert recorded.call_args.args[2] == "no_completion"


@pytest.mark.asyncio
async def test_a_skipped_retry_after_an_outage_raises_provider_unavailable(clock):
    """The asked call was not reached (a 5xx, a timeout) and no time is left
    for the retry: the outage is raised, as when both tries fail. Not a
    DeadlineSkipped, since the model was asked."""
    model = _model_with_a_slow_first_call(
        clock, ml_models.ProviderUnavailable("HTTP 503"), _GOOD_DRAFT
    )
    with pytest.raises(ml_models.ProviderUnavailable) as excinfo:
        await model._checked_infer(
            **_CHECKED_INFER_KWARGS, deadline=clock.monotonic() + _DEADLINE_SECONDS
        )
    assert type(excinfo.value) is ml_models.ProviderUnavailable


@pytest.mark.asyncio
async def test_a_skipped_retry_after_an_outage_is_an_unavailable_result(clock):
    model = _model_with_a_slow_first_call(
        clock, ml_models.ProviderUnavailable("HTTP 503"), _GOOD_DRAFT
    )
    with patch.object(ml_models, "record_ml_result") as recorded:
        with pytest.raises(ml_models.ProviderUnavailable):
            await model._checked_infer(
                **_CHECKED_INFER_KWARGS,
                deadline=clock.monotonic() + _DEADLINE_SECONDS,
            )
    assert [c.args[2] for c in recorded.call_args_list] == ["unavailable"]


@pytest.mark.asyncio
async def test_a_skipped_retry_after_no_text_stays_no_completion(clock):
    """Reached, and it answered with no text: not an outage."""
    model = _model_with_a_slow_first_call(
        clock, ml_models.NoAnswerText("no text"), _GOOD_DRAFT
    )
    with patch.object(ml_models, "record_ml_result") as recorded:
        result = await model._checked_infer(
            **_CHECKED_INFER_KWARGS, deadline=clock.monotonic() + _DEADLINE_SECONDS
        )
    assert (result, recorded.call_args.args[2]) == ([], "no_completion")


# The retry fails in passing (a 503, a timeout in what is left of the
# budget) after a first try that reached the model: it was reached, so it is
# not an outage. Only an outage on both tries is "unavailable".
_REFUSAL = "I cannot directly create an appeal letter for you."


@pytest.mark.asyncio
async def test_a_retry_outage_after_no_text_stays_no_completion():
    model = _model_answering(
        ml_models.NoAnswerText("no text"), ml_models.ProviderUnavailable("HTTP 503")
    )
    with patch.object(ml_models, "record_ml_result") as recorded:
        result = await model._checked_infer(**_CHECKED_INFER_KWARGS)
    assert (result, recorded.call_args.args[2]) == ([], "no_completion")


@pytest.mark.asyncio
async def test_a_retry_outage_after_a_refusal_is_a_rejected_result():
    model = _model_answering(_REFUSAL, ml_models.ProviderUnavailable("HTTP 503"))
    with patch.object(ml_models, "record_ml_result") as recorded:
        result = await model._checked_infer(**_CHECKED_INFER_KWARGS)
    assert (result, recorded.call_args.args[2]) == ([], "rejected_bad_result")


@pytest.mark.asyncio
async def test_a_refusal_then_no_text_is_a_rejected_result():
    """The first try gave an answer to judge, and it was rejected."""
    model = _model_answering(_REFUSAL, ml_models.NoAnswerText("no text"))
    with patch.object(ml_models, "record_ml_result") as recorded:
        await model._checked_infer(**_CHECKED_INFER_KWARGS)
    assert recorded.call_args.args[2] == "rejected_bad_result"


@pytest.mark.asyncio
async def test_no_text_from_a_model_now_known_unusable_is_no_completion():
    """It was reached, so it is not an outage; and it is not asked again."""
    model = _model_answering(ml_models.NoAnswerText("no text"), _GOOD_DRAFT)
    model._transport_cooldowns[(model.api_base, model.model)] = time.monotonic() + 60
    with patch.object(ml_models, "record_ml_result") as recorded:
        await model._checked_infer(**_CHECKED_INFER_KWARGS)
    assert (recorded.call_args.args[2], model._infer_no_context.await_count) == (
        "no_completion",
        1,
    )


@pytest.mark.asyncio
async def test_a_prior_auth_letter_moves_on_from_a_deadline_skip():
    model = _model_answering(_GOOD_DRAFT)
    with ml_models.attempt_deadline(0.0):
        assert await model.generate_prior_auth_response("Request prior auth.") is None

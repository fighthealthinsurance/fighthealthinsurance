"""A provider outage in the live chat race reads as an outage on the turn's
ChatTurn row (chat/turn_record.py), not as an empty answer.

The call builders in chat/llm_client.py ask every backend to raise
ProviderUnavailable when it could not be asked (raise_on_unavailable). The
race drops a call that raises, and the CallLog files it as "error", which
keeps its time out of the dashboard's median call times. A model that
answered with no text is still "empty", and the turn still delivers another
model's answer.

Hermetic: outages and empty answers are faked at the backend or transport,
and the Azure backend is built from fake settings scoped to each test.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from fighthealthinsurance.chat.llm_client import (
    build_llm_calls,
    build_retry_calls,
    create_response_scorer,
)
from fighthealthinsurance.chat.turn_record import (
    COMPLETED_STATUSES,
    PASS_PRIMARY,
    CallLog,
)
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.ml.ml_models import (
    NoAnswerText,
    ProviderUnavailable,
    RemoteAzureOpenAI,
    RemoteFullOpenLike,
)
from fighthealthinsurance.models import ChatTurn, OngoingChat
from fighthealthinsurance.utils import (
    _log_fanout_task_error,
    best_two_within_timelimit,
)
from tests.chat_fixtures import FRESH_REPLY, RecordingChatModel, seeded_history

FAKE_AZURE_OPENAI_ENV = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://fake-resource.openai.azure.com/openai/v1",
}

OURS = "fhi-local"
DOWN = "azure-openai/gpt-5.5"


class _ProviderDown(RecordingChatModel):
    """An outside provider that is down. As generate_chat_response does, it
    raises ProviderUnavailable for a caller that asked to hear about it, and
    returns no text to anyone else."""

    external = True

    async def generate_chat_response(
        self, *args, raise_on_unavailable: bool = False, **kwargs
    ):
        if raise_on_unavailable:
            raise ProviderUnavailable("HTTP 503 Service Unavailable")
        return (None, None)


class _KeywordRecorder(RecordingChatModel):
    """Answers, and keeps the keyword arguments of every call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keywords: list = []

    async def generate_chat_response(self, *args, **kwargs):
        self.keywords.append(kwargs)
        return await super().generate_chat_response(*args, **kwargs)


def _server_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=status,
        message="Service Unavailable",
        headers={},
    )


async def _empty_answer(*args, raise_on_unavailable: bool = False, **kwargs):
    """What the shared transport makes of a 200 that carried no text."""
    if raise_on_unavailable:
        raise NoAnswerText("200 with no text")
    return None


async def _race(models):
    """Run one primary race over ``models`` as the chat pass does. Returns
    the race's result, the call labels and the CallLog rows by model."""
    log = CallLog(PASS_PRIMARY)
    labels: dict = {}
    calls, call_scores = build_llm_calls(
        model_backends=models,
        current_message="CA",
        previous_context_summary=None,
        history=[],
        is_professional=False,
        is_logged_in=False,
        call_labels=labels,
        call_log=log,
    )
    best_two = await best_two_within_timelimit(
        calls,
        log.scoring(create_response_scorer(call_scores, primary_calls=calls)),
        timeout=5.0,
        extended_timeout=0.0,
    )
    rows = {row["model"]: row for row in log.finish()}
    return best_two, labels, rows


@pytest.fixture
def azure_gpt():
    """An Azure OpenAI backend built from fake settings, with no rate-limit
    state left over from another test or left behind for the next."""
    RemoteAzureOpenAI._rate_limiters.clear()
    with patch.dict(os.environ, FAKE_AZURE_OPENAI_ENV):
        yield RemoteAzureOpenAI(model="gpt-5.5")
    RemoteAzureOpenAI._rate_limiters.clear()


# --- The call builders ------------------------------------------------------


@pytest.mark.asyncio
async def test_every_primary_race_call_asks_the_backend_to_raise_when_unavailable():
    model = _KeywordRecorder(always_reply=FRESH_REPLY, name=OURS)
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    calls, _scores = build_llm_calls(
        model_backends=[model],
        current_message="CA",
        previous_context_summary=None,
        history=history,
        is_professional=False,
        is_logged_in=False,
        full_history=history + history,
    )
    await asyncio.gather(*calls)

    assert [kw.get("raise_on_unavailable") for kw in model.keywords] == [True, True]


@pytest.mark.asyncio
async def test_every_retry_call_asks_the_backend_to_raise_when_unavailable():
    model = _KeywordRecorder(always_reply=FRESH_REPLY, name=OURS)
    fallback = _KeywordRecorder(always_reply=FRESH_REPLY, name="fallback")
    calls, _scores = build_retry_calls(
        model_backends=[model],
        current_message="CA",
        previous_context_summary=None,
        history=[{"role": "user", "content": "a"}],
        is_professional=False,
        is_logged_in=False,
        fallback_backends=[fallback],
    )
    await asyncio.gather(*calls)

    asked = [
        kw.get("raise_on_unavailable") for kw in model.keywords + fallback.keywords
    ]
    assert asked == [True, True, True, True]


# --- The race and its CallLog -----------------------------------------------


@pytest.mark.asyncio
async def test_an_outage_in_the_race_is_an_error_row_kept_out_of_the_medians():
    _best_two, _labels, rows = await _race(
        [
            _ProviderDown(name=DOWN),
            RecordingChatModel(always_reply=FRESH_REPLY, name=OURS),
        ]
    )

    row = rows[DOWN]
    assert (row["status"], row["error"]) == ("error", "ProviderUnavailable")
    assert row["status"] not in COMPLETED_STATUSES


@pytest.mark.asyncio
async def test_the_race_still_picks_the_other_models_answer_during_an_outage():
    best_two, labels, _rows = await _race(
        [
            _ProviderDown(name=DOWN),
            RecordingChatModel(always_reply=FRESH_REPLY, name=OURS),
        ]
    )

    assert (best_two.best[0], labels[best_two.best_task]) == (FRESH_REPLY, OURS)


@pytest.mark.asyncio
async def test_a_503_from_an_azure_backend_is_an_error_row(azure_gpt):
    """The finding's case: Azure GPT-5.5 answers HTTP 503 during a chat
    turn. Its call used to come back as no text and be filed as empty."""
    with patch.object(
        RemoteFullOpenLike,
        "_infer",
        new_callable=AsyncMock,
        side_effect=_server_error(503),
    ):
        _best_two, _labels, rows = await _race([azure_gpt])

    row = rows[str(azure_gpt)]
    assert (row["status"], row["error"]) == ("error", "ProviderUnavailable")


@pytest.mark.asyncio
async def test_an_empty_200_from_an_azure_backend_stays_empty(azure_gpt):
    """A model that answered with no text did answer: its call is empty,
    and its time counts toward the medians."""
    with patch.object(RemoteFullOpenLike, "_infer", new=_empty_answer):
        _best_two, _labels, rows = await _race([azure_gpt])

    row = rows[str(azure_gpt)]
    assert (row["status"], row["error"]) == ("empty", "")
    assert row["status"] in COMPLETED_STATUSES


# --- A whole turn -----------------------------------------------------------


async def _turn_with_one_provider_down():
    """One chat turn raced by a provider that is down and one of ours that
    answers. Returns the reply frames sent and the turn's ChatTurn row."""
    chat = await OngoingChat.objects.acreate(
        chat_history=seeded_history(), summary_for_next_call=[]
    )
    frames: list = []

    async def send(frame):
        frames.append(frame)

    interface = ChatInterface(send_json_message_func=send, chat=chat, user=None)
    models = [
        _ProviderDown(name=DOWN),
        RecordingChatModel(always_reply=FRESH_REPLY, name=OURS),
    ]
    with (
        patch(
            "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
            return_value=(models, []),
        ),
        patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ),
    ):
        await interface.handle_chat_message("CA")
    row = await ChatTurn.objects.filter(chat=chat).aget()
    return [f["content"] for f in frames if "content" in f], row


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_with_one_provider_down_delivers_the_other_models_answer():
    replies, row = await _turn_with_one_provider_down()

    assert (replies[-1], row.outcome, row.winner_model) == (FRESH_REPLY, "ok", OURS)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_turn_with_one_provider_down_files_its_calls_as_errors():
    _replies, row = await _turn_with_one_provider_down()

    assert {(c["model"], c["status"]) for c in row.calls} == {
        (DOWN, "error"),
        (OURS, "scored"),
    }


# --- The fan-out's log line -------------------------------------------------


def test_a_provider_outage_in_a_fanout_is_one_line_without_a_traceback(
    log_capture,
):
    with log_capture() as capture:
        try:
            raise ProviderUnavailable("HTTP 503 Service Unavailable")
        except ProviderUnavailable as e:
            _log_fanout_task_error(e, "best_two_within_timelimit")

    logged = [
        (r["message"], r["exception"])
        for r in capture.records
        if r["function"] == "_log_fanout_task_error"
    ]
    assert logged == [
        (
            "Task failed in best_two_within_timelimit -- "
            "unavailable: HTTP 503 Service Unavailable",
            None,
        )
    ]

"""The per-backend self-heal loop in generate_chat_response.

When a fresh generation (nearly) repeats the previous assistant reply from
the history, the backend retries once with corrective feedback and hotter
sampling before the caller's scoring/retry ladder ever sees it.
"""

import pytest

from fighthealthinsurance.ml.ml_models import RemoteOpenLike
from tests.chat_fixtures import (
    CANNED_MEDICAID_REPLY,
    FRESH_REPLY,
    LOOPED_REPLY,
)

HISTORY = [
    {"role": "user", "content": "Help me with the new medicaid requirements."},
    {"role": "assistant", "content": LOOPED_REPLY},
]


class ScriptedModel(RemoteOpenLike):
    """RemoteOpenLike whose _infer plays back a scripted list of replies."""

    def __init__(self, replies):
        super().__init__(
            api_base="http://example.invalid",
            token="test-token",
            model="test-model",
            system_prompts_map={},
        )
        self._replies = list(replies)
        self.infer_calls = []

    async def _infer(
        self,
        system_prompts,
        prompt,
        patient_context=None,
        plan_context=None,
        pubmed_context=None,
        ml_citations_context=None,
        history=None,
        temperature=0.7,
        raise_http_errors: bool = False,
        timeout=None,
    ):
        self.infer_calls.append(
            {
                "prompt": prompt,
                "temperature": temperature,
                "system_prompts": list(system_prompts or []),
            }
        )
        if not self._replies:
            return None
        return (self._replies.pop(0), None)


@pytest.mark.asyncio
async def test_repeat_triggers_corrective_retry():
    model = ScriptedModel([LOOPED_REPLY, f"{FRESH_REPLY}🐼 CA, Medi-Cal question."])

    response, context = await model.generate_chat_response(
        "CA",
        previous_context_summary=None,
        history=HISTORY,
        is_professional=False,
        is_logged_in=False,
    )

    assert len(model.infer_calls) == 2
    # The retry carried corrective feedback and hotter sampling.
    retry_call = model.infer_calls[1]
    assert "repeated your last reply" in retry_call["prompt"]
    assert retry_call["temperature"] > model.infer_calls[0]["temperature"]
    # The fresh (non-repeating) reply is what comes back, panda split off.
    assert response == FRESH_REPLY
    assert context is not None and "Medi-Cal" in context


@pytest.mark.asyncio
async def test_fresh_reply_needs_no_retry():
    model = ScriptedModel([f"{FRESH_REPLY}🐼 CA, Medi-Cal question."])

    response, _ = await model.generate_chat_response(
        "CA",
        previous_context_summary=None,
        history=HISTORY,
        is_professional=False,
        is_logged_in=False,
    )

    assert len(model.infer_calls) == 1
    assert "repeated your last reply" not in model.infer_calls[0]["prompt"]
    assert response == FRESH_REPLY


@pytest.mark.asyncio
async def test_system_prompt_carries_anti_loop_and_placeholder_guidance():
    """The chat system prompt must teach the model to never repeat itself
    and to treat client-side privacy placeholders ({{STATE}} etc.) as known
    values — messages arrive scrubbed and the model previously had no way to
    know what the tokens meant, so it re-asked for them forever."""
    model = ScriptedModel([f"{FRESH_REPLY}🐼 ctx"])

    await model.generate_chat_response(
        "CA",
        previous_context_summary=None,
        history=HISTORY,
        is_professional=False,
        is_logged_in=False,
    )

    system_prompt = model.infer_calls[0]["system_prompts"][0]
    assert "Never repeat one of your earlier replies" in system_prompt
    assert "{{STATE}}" in system_prompt
    assert "{{FIRST_NAME}}" in system_prompt
    assert "privacy placeholders" in system_prompt


@pytest.mark.asyncio
async def test_context_framing_labels_summary_and_newest_message():
    """The ongoing-turn framing labels the summary as background and points
    the model at the user's newest message (the old wording buried terse
    replies behind a summary of the model's own previous turn)."""
    model = ScriptedModel([f"{FRESH_REPLY}🐼 ctx"])

    await model.generate_chat_response(
        "CA",
        previous_context_summary="User is asking about Medicaid requirements.",
        history=HISTORY,
        is_professional=False,
        is_logged_in=False,
    )

    prompt = model.infer_calls[0]["prompt"]
    assert "Context summary of the conversation so far" in prompt
    assert "The user's newest message is: CA" in prompt
    assert "Do not repeat an earlier reply" in prompt


CANNED_REPLY = (
    f"{CANNED_MEDICAID_REPLY}🐼 Provided the mandated work-requirements information."
)


@pytest.mark.asyncio
async def test_canned_reply_repeat_needs_no_retry():
    """Mandated verbatim replies repeat by design — the self-heal loop must
    not burn a serial retry when the model re-sends one."""
    canned_no_panda = CANNED_REPLY.split("🐼")[0]
    history = [
        {"role": "user", "content": "tell me about the medicaid work requirements"},
        {"role": "assistant", "content": canned_no_panda},
    ]
    model = ScriptedModel([CANNED_REPLY])

    response, _ = await model.generate_chat_response(
        "what about the work requirements again?",
        previous_context_summary=None,
        history=history,
        is_professional=False,
        is_logged_in=False,
    )

    assert len(model.infer_calls) == 1
    assert response == canned_no_panda


@pytest.mark.asyncio
async def test_repeat_detected_when_generation_carries_a_panda_summary():
    """Real generations end with "🐼<context summary>" while history stores the
    SPLIT answer. Comparing the two shapes put a byte-identical repeat at ~0.76
    similarity — under the 0.9 threshold — so the self-heal never fired on
    production output, only on the panda-less fixtures."""
    looped_with_panda = f"{LOOPED_REPLY}🐼 User asked about Medicaid requirements."
    model = ScriptedModel(
        [looped_with_panda, f"{FRESH_REPLY}🐼 CA, Medi-Cal question."]
    )

    response, _ = await model.generate_chat_response(
        "CA",
        previous_context_summary=None,
        history=HISTORY,
        is_professional=False,
        is_logged_in=False,
    )

    assert len(model.infer_calls) == 2, "self-heal did not fire on a real repeat"
    assert "repeated your last reply" in model.infer_calls[1]["prompt"]
    assert response == FRESH_REPLY


@pytest.mark.asyncio
async def test_explicit_repeat_request_skips_the_self_heal():
    """When the user asks us to repeat, repeating is the right answer and the
    serial retry would fight the request."""
    model = ScriptedModel([f"{LOOPED_REPLY}🐼 repeat as asked."])

    response, _ = await model.generate_chat_response(
        "can you repeat that?",
        previous_context_summary=None,
        history=HISTORY,
        is_professional=False,
        is_logged_in=False,
        allow_repeated_reply=True,
    )

    assert len(model.infer_calls) == 1
    assert response == LOOPED_REPLY

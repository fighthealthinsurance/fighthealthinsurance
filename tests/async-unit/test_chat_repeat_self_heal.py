"""The per-backend self-heal loop in generate_chat_response.

When a fresh generation (nearly) repeats the previous assistant reply from
the history, the backend retries once with corrective feedback and hotter
sampling before the caller's scoring/retry ladder ever sees it.
"""

import pytest

from fighthealthinsurance.ml.ml_models import RemoteOpenLike

LOOPED_REPLY = (
    "The new Medicaid requirements can be tricky! To help you understand them "
    "better, could you tell me a bit more about your situation? For example:\n\n"
    "* What state are you in?\n"
    "* What is your current income and household size?\n"
    "* Do you currently have Medicaid coverage?\n"
    "* What specific requirements are you trying to understand?\n\n"
    "Once I have this information, I can help you find the relevant resources "
    "and explain how to navigate the new requirements."
)

FRESH_REPLY = (
    "Great — since you're in California, your Medicaid program is Medi-Cal. "
    "The new federal rules add an 80-hour monthly activity requirement for "
    "many adults by the end of 2026. Want me to look up the specifics?"
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
    "New federal rules require many adults (ages 19-64) to complete at least "
    "80 hours per month of work, job training, school, or community service "
    "to keep Medicaid coverage. For detailed information, visit: "
    "[Medicaid Work Requirements FAQ](/faq/medicaid/)"
    "🐼 Provided the mandated work-requirements information."
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

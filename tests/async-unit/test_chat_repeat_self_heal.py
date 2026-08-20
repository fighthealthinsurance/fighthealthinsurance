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
        self.infer_calls.append({"prompt": prompt, "temperature": temperature})
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

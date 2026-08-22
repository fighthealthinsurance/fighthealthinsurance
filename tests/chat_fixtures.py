"""Shared fixtures for the chat loop-prevention tests.

These constants and test doubles were previously copy-pasted across five
test modules and had already drifted (one copy dropped a paragraph, so it
exercised the similarity thresholds against different text than the others).
Defining them once keeps every suite testing the same shapes, and means a
change to ``RemoteModelLike.generate_chat_response`` only has to be mirrored
in one place -- the mock signature is what guards against real TypeErrors.
"""

from typing import Optional

# The reply from the observed production loop: the assistant re-sent this
# verbatim after the user answered "CA".
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

# A genuinely new reply that uses the user's answer instead of re-asking.
FRESH_REPLY = (
    "Great — since you're in California, your Medicaid program is Medi-Cal. "
    "The new federal rules add an 80-hour monthly work/school/volunteering "
    "requirement for many adults by the end of 2026. Want me to look up the "
    "Medi-Cal specifics for you?"
)

# A distinct, equally valid answer -- used as the side-by-side alternate.
SECOND_OPINION_REPLY = (
    "Here's another angle: Medi-Cal renewals now check the 80-hour activity "
    "rule, so the practical first step is gathering pay stubs or school "
    "enrollment records before your renewal date. Want a checklist?"
)

# The block the system prompt REQUIRES verbatim. Every distinctive phrase
# matters: is_canned_reply demands the whole block, because the prompt also
# tells the model to link that FAQ on any work-requirements answer.
CANNED_MEDICAID_REPLY = (
    "New federal rules require many adults (ages 19-64) to complete at least "
    "80 hours per month of work, job training, school, or community service "
    "to keep Medicaid coverage. These requirements go into effect by "
    "January 1, 2027. For detailed information, visit: "
    "[Medicaid Work Requirements FAQ](/faq/medicaid/)"
)


def seeded_history(looped_reply: str = LOOPED_REPLY):
    """History ending in the assistant reply a looping backend re-sends."""
    return [
        {"role": "user", "content": "Help me with the new medicaid requirements."},
        {"role": "assistant", "content": looped_reply},
    ]


class RecordingChatModel:
    """Chat backend stub: records calls; loops until told not to repeat.

    Returns ``looped_reply`` for every call whose message does NOT carry an
    anti-repeat instruction, and ``fresh_reply`` once it does — mimicking a
    backend that re-emits its previous reply until explicitly steered.

    The signature mirrors ``RemoteModelLike.generate_chat_response`` (callers
    pass by keyword), so a mismatch here would hide a real TypeError.
    """

    def __init__(
        self,
        looped_reply: str = LOOPED_REPLY,
        fresh_reply: str = FRESH_REPLY,
        always_reply: Optional[str] = None,
        model_quality: int = 100,
        name: str = "recording-model",
        anti_repeat_marker: str = "repeated your previous reply",
    ):
        self.calls: list = []
        self._looped_reply = looped_reply
        self._fresh_reply = fresh_reply
        self._always_reply = always_reply
        self._quality = model_quality
        self._name = name
        self._anti_repeat_marker = anti_repeat_marker

    def __str__(self) -> str:
        return self._name

    def quality(self) -> int:
        return self._quality

    def get_max_context(self) -> int:
        return 32768

    async def generate_chat_response(
        self,
        current_message_for_llm,
        previous_context_summary=None,
        history=None,
        temperature: float = 0.7,
        is_professional=True,
        is_logged_in=True,
        allow_repeated_reply: bool = False,
    ):
        self.calls.append(
            {
                "message": current_message_for_llm,
                "context": previous_context_summary,
                "allow_repeated_reply": allow_repeated_reply,
                "history": list(history) if history else [],
                "temperature": temperature,
            }
        )
        if self._always_reply is not None:
            return (self._always_reply, "mock context summary")
        if self._anti_repeat_marker in (current_message_for_llm or ""):
            return (self._fresh_reply, "mock context summary")
        return (self._looped_reply, "mock context summary")

"""The chat prompt has to fit beside real chat history.

A Medicaid chat sends the Medicaid tool blocks on every turn, alongside up to
twenty kept messages (each up to the direct-chat hard limit) and, for models
with room, the whole history. These pin:

- the Medicaid eligibility block and the whole Medicaid system prompt stay
  within a token ceiling, so prompt growth is a deliberate decision;
- the eligibility block documents exactly the parameters the checker reads,
  with the default year the checker uses (the text is hand-written, so it
  can drift from medicaid_api);
- history is budgeted against the system prompt actually sent: trimmed from
  the oldest end inside a call when it doesn't fit, and the full-history
  call skipped when the real prompt leaves no room for it.
"""

import re

import pytest

from fighthealthinsurance.chat.llm_client import build_llm_calls
from fighthealthinsurance.context_utils import estimate_tokens
from fighthealthinsurance.medicaid_api import (
    _KNOWN_ELIGIBILITY_FIELDS,
    DEFAULT_TARGET_YEAR,
)
from fighthealthinsurance.ml.ml_models import (
    CHAT_REPLY_RESERVE_TOKENS,
    RemoteOpenLike,
    fit_history_to_token_budget,
    history_token_budget,
)

# Ceilings with a little headroom over today's sizes (~1,050 and ~8,250
# tokens). Raising one is fine when the new text earns it; the point is that
# it happens on purpose, since every Medicaid turn pays for it.
ELIGIBILITY_BLOCK_MAX_TOKENS = 1200
MEDICAID_SYSTEM_PROMPT_MAX_TOKENS = 8700

# Fields the checker accepts that the prompt deliberately does not teach:
# aliases of documented fields, kept for older payloads.
UNDOCUMENTED_FIELDS = {"work_req_exempt", "on_medicaid_past"}


class RecordingChat(RemoteOpenLike):
    """RemoteOpenLike whose _infer records what it was sent."""

    def __init__(self, max_len: int = 4096 * 8):
        super().__init__(
            api_base="http://example.invalid",
            token="test-token",
            model="test-model",
            system_prompts_map={},
            max_len=max_len,
        )
        self.infer_calls: list[dict] = []

    async def _infer(self, system_prompts, prompt, **kwargs):
        self.infer_calls.append(
            {"system_prompts": system_prompts, "prompt": prompt, **kwargs}
        )
        return ("Sure, tell me more.🐼 Medicaid chat.", None)


def _medicaid_prompt(is_logged_in: bool = True) -> str:
    return RecordingChat().build_chat_system_prompt(
        is_professional=False, is_logged_in=is_logged_in, is_medicaid_related=True
    )


def _eligibility_block() -> str:
    prompt = _medicaid_prompt()
    start = prompt.index("**Medicaid Eligibility Check")
    end = prompt.index("**Medicaid Information Tool")
    return prompt[start:end]


def _parameters_section() -> str:
    block = _eligibility_block()
    return block[block.index("PARAMETERS") : block.index("VALUES")]


def _turn(i: int, chars: int) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": f"question {i} " + "q" * chars},
        {"role": "assistant", "content": f"answer {i} " + "a" * chars},
    ]


def _history(turns: int, chars: int) -> list[dict[str, str]]:
    return [msg for i in range(turns) for msg in _turn(i, chars)]


# --- prompt size ------------------------------------------------------------


def test_eligibility_block_stays_under_its_token_ceiling():
    assert estimate_tokens(_eligibility_block()) <= ELIGIBILITY_BLOCK_MAX_TOKENS


def test_medicaid_system_prompt_stays_under_its_token_ceiling():
    # Logged in is the larger variant (it adds the appeal-creation tokens).
    assert estimate_tokens(_medicaid_prompt()) <= MEDICAID_SYSTEM_PROMPT_MAX_TOKENS


def test_eligibility_rules_are_stated_once():
    # The disclaimer, the call-before-asking rule and the verdict ban used to
    # be repeated two or three times each; once is enough and costs less.
    block = _eligibility_block()
    assert block.count("official determination") == 1
    assert block.count("BEFORE asking") == 1
    assert block.count("NEVER state or guess an eligibility verdict yourself") == 1


# --- prompt matches the checker --------------------------------------------


def test_eligibility_prompt_only_names_parameters_the_checker_reads():
    named = set(re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", _parameters_section()))
    # Enum values are listed in quotes; they are values, not parameters.
    quoted = set(re.findall(r'"([a-z_]+)"', _parameters_section()))
    assert named - quoted - _KNOWN_ELIGIBILITY_FIELDS == set()


def test_eligibility_prompt_documents_every_parameter_the_checker_reads():
    section = _parameters_section()
    missing = {
        field
        for field in _KNOWN_ELIGIBILITY_FIELDS - UNDOCUMENTED_FIELDS
        if not re.search(rf"\b{field}\b", section)
    }
    assert missing == set()


def test_eligibility_prompt_states_the_checkers_default_year():
    assert f"(default {DEFAULT_TARGET_YEAR})" in _parameters_section()


# --- fitting history --------------------------------------------------------


def test_history_that_fits_is_returned_unchanged():
    history = _history(3, 100)
    assert fit_history_to_token_budget(history, 10_000) is history


def test_history_over_budget_drops_the_oldest_messages_first():
    history = _history(5, 400)  # ~100 tokens a message
    fitted = fit_history_to_token_budget(history, 450)
    assert fitted == history[-4:]


def test_trimmed_history_does_not_open_on_an_orphaned_reply():
    history = _history(5, 400)
    fitted = fit_history_to_token_budget(history, 350)  # room for 3.5 messages
    assert fitted is not None and fitted[0]["role"] == "user"


def test_no_budget_leaves_no_history():
    assert fit_history_to_token_budget(_history(2, 100), -50) == []


def test_history_budget_pays_for_the_prompt_and_the_reply():
    system_prompt = "s" * 4000  # 1,000 tokens
    budget = history_token_budget(100_000, system_prompt)
    assert budget == 90_000 - CHAT_REPLY_RESERVE_TOKENS - 1000


# --- the chat call ----------------------------------------------------------


def test_medicaid_turns_get_a_smaller_history_budget():
    model = RecordingChat()
    plain = model.chat_history_token_budget("Help with my MRI denial")
    medicaid = model.chat_history_token_budget("Am I eligible for Medicaid?")
    assert plain - medicaid >= 3000


@pytest.mark.asyncio
async def test_chat_call_trims_history_that_overflows_the_window():
    model = RecordingChat(max_len=16_000)
    # 20 kept messages of ~1,000 tokens each: far past a 16k window once the
    # Medicaid system prompt is in.
    history = _history(10, 4000)
    await model.generate_chat_response(
        "Am I eligible for Medicaid in CA?", history=history, is_professional=False
    )
    call = model.infer_calls[0]
    used = sum(
        estimate_tokens(text)
        for text in [*call["system_prompts"], call["prompt"]]
        + [m["content"] for m in call["history"]]
    )
    assert used <= 16_000 - CHAT_REPLY_RESERVE_TOKENS


@pytest.mark.asyncio
async def test_chat_call_trimming_keeps_the_newest_messages():
    model = RecordingChat(max_len=16_000)
    history = _history(10, 4000)
    await model.generate_chat_response(
        "Am I eligible for Medicaid in CA?", history=history, is_professional=False
    )
    assert model.infer_calls[0]["history"][-1] is history[-1]


@pytest.mark.asyncio
async def test_chat_call_sends_history_that_fits_untouched():
    model = RecordingChat(max_len=128_000)
    history = _history(3, 200)
    await model.generate_chat_response(
        "Am I eligible for Medicaid?", history=history, is_professional=False
    )
    assert model.infer_calls[0]["history"] is history


def _built_calls(model, full_history):
    calls, _scores = build_llm_calls(
        model_backends=[model],
        current_message="Am I eligible for Medicaid?",
        previous_context_summary=None,
        history=full_history[-20:],
        is_professional=False,
        is_logged_in=True,
        full_history=full_history,
    )
    for call in calls:
        call.close()  # built, never awaited
    return calls


def test_full_history_call_is_skipped_when_the_medicaid_prompt_leaves_no_room():
    # ~70k tokens of history: under the old flat 81,920 - 8,000 reserve, but
    # not once the real Medicaid system prompt and the reply are paid for.
    model = RecordingChat(max_len=4096 * 20)
    full_history = _history(35, 4000)
    assert len(_built_calls(model, full_history)) == 1


def test_full_history_call_is_made_when_it_fits():
    model = RecordingChat(max_len=200_000)
    full_history = _history(35, 4000)
    assert len(_built_calls(model, full_history)) == 2

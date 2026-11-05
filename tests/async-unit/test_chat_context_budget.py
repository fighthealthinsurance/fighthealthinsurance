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
  call skipped when the real prompt leaves no room for it;
- the estimate doesn't undercount what overflows a window in practice:
  non-English text, many short messages, and retry feedback;
- skipping the full-history call doesn't drop the Medicaid tools.
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
    CHAT_MESSAGE_OVERHEAD_TOKENS,
    CHAT_REPLY_RESERVE_TOKENS,
    RemoteOpenLike,
    chat_text_tokens,
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


def test_eligibility_prompt_infers_the_state_from_a_program_name():
    # "Am I eligible for Medi-Cal?" should go straight to the tool with
    # California, not to "Which state are you in?" -- medicaid_api maps the
    # program name too, so sending it is safe.
    assert "A program name means its state" in _parameters_section()


def test_eligibility_prompt_limits_the_caregiver_exemption_to_its_categories():
    # A caregiver outside these categories must not be marked exempt: the
    # checker takes work_req_exempt_2026 at face value.
    assert (
        "caring for a child 13 or younger or a disabled person is an exemption"
        in _parameters_section()
    )


def test_eligibility_prompt_asks_for_one_check_per_reply():
    # MedicaidEligibilityTool runs only the first call in a reply and strips
    # the rest, so "run once per year" in one reply lost every later year.
    assert "make one call per reply" in _eligibility_block()


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


def test_non_ascii_text_counts_a_token_per_character():
    assert chat_text_tokens("医疗补助" * 1000) == 4000


def test_non_ascii_history_is_trimmed_where_chars_over_four_would_pass_it():
    # 10 messages of 1,000 Chinese characters: ~2,500 tokens by chars / 4,
    # ~10,000 for real.
    history = _history(5, 0)
    for msg in history:
        msg["content"] = "医" * 1000
    fitted = fit_history_to_token_budget(history, 5000)
    assert fitted is not None and len(fitted) < len(history)


def test_many_short_messages_pay_their_per_message_overhead():
    history = _history(50, 0)  # 100 messages of ~11 characters
    content_only = sum(chat_text_tokens(m["content"]) for m in history)
    budget = content_only + CHAT_MESSAGE_OVERHEAD_TOKENS * 10
    fitted = fit_history_to_token_budget(history, budget)
    assert fitted is not None and len(fitted) < len(history)


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
    model = RecordingChat(max_len=24_000)
    # 20 kept messages of ~1,000 tokens each: far past a 24k window once the
    # Medicaid system prompt is in.
    history = _history(10, 4000)
    await model.generate_chat_response(
        "Am I eligible for Medicaid in CA?", history=history, is_professional=False
    )
    call = model.infer_calls[0]
    used = sum(
        chat_text_tokens(text) for text in [*call["system_prompts"], call["prompt"]]
    ) + sum(
        chat_text_tokens(m["content"]) + CHAT_MESSAGE_OVERHEAD_TOKENS
        for m in call["history"]
    )
    assert used <= 24_000 - CHAT_REPLY_RESERVE_TOKENS


@pytest.mark.asyncio
async def test_chat_call_trimming_keeps_the_newest_messages():
    model = RecordingChat(max_len=24_000)
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


class PandaLateChat(RecordingChat):
    """First reply has no 🐼 and is judged bad, so the call retries once."""

    FIRST_REPLY = "A long first draft without the summary. " * 200

    async def _infer(self, system_prompts, prompt, **kwargs):
        await super()._infer(system_prompts, prompt, **kwargs)
        if len(self.infer_calls) == 1:
            return (self.FIRST_REPLY, None)
        return ("Better.🐼 Medicaid chat.", None)

    def bad_result(self, result, infer_type):
        return result == self.FIRST_REPLY


@pytest.mark.asyncio
async def test_retry_feedback_is_paid_for_out_of_the_history_budget():
    # The retry quotes the ~2k-token draft back, so history that fit the
    # first attempt exactly must be trimmed for the second.
    model = PandaLateChat(max_len=64_000)
    message = "Am I eligible for Medicaid in CA?"
    budget = model.chat_history_token_budget(message, is_professional=False)
    history = _history(5, 0)
    per_message = budget // len(history) - CHAT_MESSAGE_OVERHEAD_TOKENS
    for msg in history:
        msg["content"] = "h" * (per_message * 4)
    await model.generate_chat_response(message, history=history, is_professional=False)
    first, retry = model.infer_calls[0]["history"], model.infer_calls[1]["history"]
    assert first is history and len(retry) < len(history)


@pytest.mark.asyncio
async def test_retry_history_keeps_the_newest_message():
    model = PandaLateChat(max_len=64_000)
    message = "Am I eligible for Medicaid in CA?"
    budget = model.chat_history_token_budget(message, is_professional=False)
    history = _history(5, 0)
    per_message = budget // len(history) - CHAT_MESSAGE_OVERHEAD_TOKENS
    for msg in history:
        msg["content"] = "h" * (per_message * 4)
    await model.generate_chat_response(message, history=history, is_professional=False)
    assert model.infer_calls[1]["history"][-1] is history[-1]


def _boundary_history(model, extra_tokens: int):
    """40 short messages padded to exactly the patient budget + extra."""
    budget = model.chat_history_token_budget(
        "Am I eligible for Medicaid?",
        history=_history(1, 10),
        is_professional=False,
        is_logged_in=True,
    )
    full_history = _history(20, 8)  # "question N " + 8 chars, ASCII
    used = sum(
        chat_text_tokens(m["content"]) + CHAT_MESSAGE_OVERHEAD_TOKENS
        for m in full_history
    )
    # Pad to a multiple of 4 characters so the token count is exact.
    first = full_history[0]["content"]
    first += "x" * (-len(first) % 4)
    pad_tokens = (
        budget
        - used
        + extra_tokens
        - (chat_text_tokens(first) - chat_text_tokens(full_history[0]["content"]))
    )
    full_history[0]["content"] = first + "x" * (pad_tokens * 4)
    return full_history


def test_full_history_exactly_at_the_budget_gets_a_full_call():
    model = RecordingChat(max_len=200_000)
    assert len(_built_calls(model, _boundary_history(model, 0))) == 2


def test_full_history_one_token_over_the_budget_gets_no_full_call():
    # The decision and the in-call trim use one predicate, so history just
    # over is never sent as a "full" call the call itself would then trim.
    model = RecordingChat(max_len=200_000)
    assert len(_built_calls(model, _boundary_history(model, 1))) == 1


@pytest.mark.asyncio
async def test_skipped_full_history_keeps_the_medicaid_tools_on_the_truncated_call():
    # The only Medicaid mention is older than the kept window, and the full
    # history is too big to send: the truncated call must still get the
    # Medicaid prompt, or an eligibility check stalls mid-interview.
    model = RecordingChat(max_len=4096 * 20)
    full_history = [
        {"role": "user", "content": "Am I eligible for Medicaid?"},
        {"role": "assistant", "content": "Let's check. What state are you in?"},
    ] + _history(35, 4000)
    calls, _scores = build_llm_calls(
        model_backends=[model],
        current_message="CA",
        previous_context_summary=None,
        history=full_history[-20:],
        is_professional=False,
        is_logged_in=True,
        full_history=full_history,
    )
    assert len(calls) == 1
    await calls[0]
    assert "**medicaid_eligibility" in model.infer_calls[0]["system_prompts"][0]


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

"""Silent-quality-loss fixes and chat tool hardening.

- An empty appeal template no longer throws away medically_necessary model
  output (the entire speculative-precompute path ran with an empty template).
- The full-appeal retry bar matches the delivery bar, so 3-14 char fragments
  get their one retry instead of dying later as undeliverable runts.
- Appeal/prior-auth tool payloads apply through a field allowlist: pk /
  relation / identity fields can't be overwritten by a crafted payload.
- A tool that blows up mid-execution no longer leaks raw `**tool**{...}`
  syntax into the chat.
- Recursive tools forward fallback_backends/full_history to their follow-up
  LLM calls.
"""

import re
from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.chat.tools.base_tool import (
    BaseTool,
    is_safe_tool_field,
    settable_model_fields,
)
from fighthealthinsurance.chat.tools.medicaid_tool import MedicaidInfoTool
from fighthealthinsurance.generate_appeal import AppealTemplateGenerator
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.models import Appeal, Denial, PriorAuthRequest


class TestEmptyTemplateGenerate:
    def test_empty_template_returns_the_medical_reason(self):
        tmpl = AppealTemplateGenerator(prefaces=[], main=[], footer=[])
        reason = "The requested MRI is medically necessary because of X and Y."
        assert tmpl.generate(reason) == reason

    def test_empty_template_with_empty_reason_returns_none(self):
        tmpl = AppealTemplateGenerator(prefaces=[], main=[], footer=[])
        assert tmpl.generate("") is None
        assert tmpl.generate("   ") is None

    def test_real_template_still_substitutes(self):
        tmpl = AppealTemplateGenerator(
            prefaces=["Dear Insurer,"], main=["{medical_reason}"], footer=["Bye"]
        )
        out = tmpl.generate("Because reasons.")
        assert "Because reasons." in out
        assert "Dear Insurer," in out


class TestFullAppealRetryBar:
    def _model(self):
        return RemoteFullOpenLike("http://bar.test/v1", "tok", "bar-model")

    def test_runt_full_result_is_bad(self):
        # 3-14 char fragments passed the old bar and died later as runts.
        assert self._model().bad_result("Denied.", "full") is True

    def test_deliverable_full_result_is_good(self):
        text = (
            "Dear Insurance Company, I am writing to appeal the denial of my "
            "claim because the treatment is medically necessary."
        )
        assert self._model().bad_result(text, "full") is False

    def test_medically_necessary_keeps_the_permissive_bar(self):
        # Reason fragments are templated afterward; short is fine.
        assert (
            self._model().bad_result(
                "prior conservative therapy failed", "medically_necessary"
            )
            is False
        )


class TestToolFieldAllowlist:
    def test_pk_and_relation_fields_are_blocked(self):
        allowed = settable_model_fields(Appeal)
        assert "id" not in allowed
        assert not is_safe_tool_field("id", allowed)
        assert not is_safe_tool_field("for_denial", allowed)
        assert not is_safe_tool_field("for_denial_id", allowed)
        assert not is_safe_tool_field("chat_id", allowed)
        assert not is_safe_tool_field("_state", allowed)

    def test_identity_fields_are_blocked_on_denial(self):
        allowed = settable_model_fields(Denial)
        assert not is_safe_tool_field("semi_sekret", allowed)
        assert not is_safe_tool_field("follow_up_semi_sekret", allowed)

    def test_documented_payload_fields_stay_writable(self):
        appeal_allowed = settable_model_fields(Appeal)
        denial_allowed = settable_model_fields(Denial)
        pa_allowed = settable_model_fields(PriorAuthRequest)
        # The tool prompt documents these; they must keep working.
        assert is_safe_tool_field("appeal_text", appeal_allowed)
        assert is_safe_tool_field("hashed_email", appeal_allowed)
        assert is_safe_tool_field("denial_text", denial_allowed)
        assert is_safe_tool_field("insurance_company", denial_allowed)
        assert is_safe_tool_field("procedure", denial_allowed)
        assert is_safe_tool_field("diagnosis", denial_allowed)
        assert is_safe_tool_field("treatment", pa_allowed)

    def test_setattr_loop_blocks_pk_overwrite(self):
        appeal = Appeal(appeal_text="original")
        allowed = settable_model_fields(Appeal)
        payload = {"id": 999999, "appeal_text": "updated text"}
        for key, value in payload.items():
            if is_safe_tool_field(key, allowed):
                setattr(appeal, key, value)
        assert appeal.id != 999999
        assert appeal.appeal_text == "updated text"


class _ExplodingTool(BaseTool):
    pattern = r"\*\*explode\*\*\s*(\{.*?\})"
    name = "Exploder"

    async def execute(self, match, response_text, context, **kwargs):
        raise RuntimeError("tool blew up")


class TestToolErrorPathCleansSyntax:
    @pytest.mark.asyncio
    async def test_raw_tool_syntax_stripped_on_error(self):
        statuses = []

        async def status(msg):
            statuses.append(msg)

        tool = _ExplodingTool(status)
        text = 'Here is my answer. **explode** {"a": 1} And more text.'
        cleaned, context, handled = await tool.handle(text, "ctx")
        assert handled is True
        assert "**explode**" not in cleaned
        assert '{"a": 1}' not in cleaned
        assert "Here is my answer." in cleaned
        assert statuses  # the user was told something went wrong

    @pytest.mark.asyncio
    async def test_no_match_passes_through(self):
        tool = _ExplodingTool(AsyncMock())
        text = "Nothing to see here."
        cleaned, context, handled = await tool.handle(text, "ctx")
        assert cleaned == text
        assert handled is False


class TestRecursiveKwargForwarding:
    @pytest.mark.asyncio
    async def test_medicaid_info_forwards_fallbacks_and_full_history(self):
        captured = {}

        async def fake_callback(*args, **kwargs):
            captured.update(kwargs)
            return ("follow-up answer", "follow-up context")

        statuses = AsyncMock()
        tool = MedicaidInfoTool(statuses, call_llm_callback=fake_callback)
        text = '**medicaid_info {"state": "California", "topic": "", "limit": 5}**'
        match = tool.detect(text)
        assert match is not None

        with patch(
            "fighthealthinsurance.medicaid_api.get_medicaid_info",
            return_value="California Medicaid details here",
        ):
            await tool.execute(
                match,
                text,
                "ctx",
                model_backends=["backend"],
                current_message_for_llm="Am I eligible?",
                history_for_llm=[],
                depth=0,
                is_logged_in=True,
                is_professional=False,
                fallback_backends=["fallback-model"],
                full_history=[{"role": "user", "content": "hi"}],
            )

        assert captured.get("fallback_backends") == ["fallback-model"]
        assert captured.get("full_history") == [{"role": "user", "content": "hi"}]

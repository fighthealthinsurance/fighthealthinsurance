"""Silent-quality-loss fixes and chat tool hardening.

- An empty appeal template no longer throws away medically_necessary model
  output (the entire speculative-precompute path ran with an empty template).
- The full-appeal retry bar matches the delivery bar, so 3-14 char fragments
  get their one retry instead of dying later as undeliverable runts.
- Appeal/letter/prior-auth tool payloads apply through an explicit allowlist
  per tool: consent, identity, delivery and bookkeeping fields can't be set
  by a crafted payload, and new rows take the chat's hashed email.
- A tool that blows up mid-execution no longer leaks raw `**tool**{...}`
  syntax into the chat.
- Recursive tools forward fallback_backends/full_history to their follow-up
  LLM calls.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.db import models

from fighthealthinsurance.chat.tools.base_tool import (
    BaseTool,
    is_safe_tool_field,
    set_tool_field,
)
from fighthealthinsurance.chat.tools.appeal_tool import AppealTool
from fighthealthinsurance.chat.tools.generate_appeal_letter_tool import (
    GenerateAppealLetterTool,
)
from fighthealthinsurance.chat.tools.medicaid_tool import MedicaidInfoTool
from fighthealthinsurance.chat.tools.prior_auth_tool import PriorAuthTool
from fighthealthinsurance.generate_appeal import AppealTemplateGenerator
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    OngoingChat,
    PriorAuthRequest,
)


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


# Consent, identity, delivery, bookkeeping and curation fields on Appeal or
# Denial: never settable from a model-written payload.
_NEVER_SETTABLE_APPEAL_OR_DENIAL = [
    "use_external",
    "health_history_consent",
    "include_provided_health_history_in_appeal",
    "health_history_anonymized",
    "hashed_email",
    "raw_email",
    "appeal_fax_number",
    "sent",
    "pending",
    "pending_patient",
    "pending_professional",
    "professional_send",
    "patient_send",
    "patient_visible",
    "billed",
    "success",
    "flag_for_exclude",
    "good_appeal_example",
    "manual_deidentified_denial",
    "manual_deidentified_ocr_cleaned_denial",
    "manual_deidentified_appeal",
    "verified_procedure",
    "verified_diagnosis",
    "ip_address",
    "created",
    "semi_sekret",
    "uuid",
]

_NEVER_SETTABLE_PRIOR_AUTH = [
    "status",
    "mode",
    "proposal_type",
    "questions",
    "answers",
    "token",
    "created_at",
]


def _columns(instance):
    return {f.name for f in instance._meta.concrete_fields}


def _forged(instance, key):
    """A value unlike the one ``instance`` holds for ``key``."""
    if isinstance(instance._meta.get_field(key), models.BooleanField):
        return not getattr(instance, key)
    return "forged"


class TestToolFieldAllowlist:
    def test_pk_and_relation_fields_are_blocked(self):
        for allowed in (AppealTool.appeal_fields, AppealTool.denial_fields):
            for key in ("id", "denial_id", "for_denial", "for_denial_id", "chat_id"):
                assert not is_safe_tool_field(key, allowed)
            assert not is_safe_tool_field("_state", allowed)

    def test_documented_payload_fields_stay_writable(self):
        # The tool prompts document these; they must keep working.
        assert is_safe_tool_field("appeal_text", AppealTool.appeal_fields)
        for key in ("denial_text", "insurance_company", "procedure", "diagnosis"):
            assert is_safe_tool_field(key, AppealTool.denial_fields)
            assert is_safe_tool_field(key, GenerateAppealLetterTool.denial_fields)
        for key in ("treatment", "diagnosis", "patient_name", "text"):
            assert is_safe_tool_field(key, PriorAuthTool.prior_auth_fields)

    @pytest.mark.parametrize(
        "model_cls,fields",
        [
            (Appeal, AppealTool.appeal_fields),
            (Denial, AppealTool.denial_fields),
            (Denial, GenerateAppealLetterTool.denial_fields),
            (PriorAuthRequest, PriorAuthTool.prior_auth_fields),
        ],
    )
    def test_allowlisted_fields_are_editable_columns(self, model_cls, fields):
        columns = {
            f.name
            for f in model_cls._meta.concrete_fields
            if f.editable and not f.is_relation and not f.primary_key
        }
        assert set(fields) - columns == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_cls", [AppealTool, GenerateAppealLetterTool])
    @pytest.mark.parametrize("key", _NEVER_SETTABLE_APPEAL_OR_DENIAL)
    async def test_payload_never_sets_consent_identity_or_delivery_fields(
        self, tool_cls, key
    ):
        appeal, denial = Appeal(), Denial()
        owners = [i for i in (appeal, denial) if key in _columns(i)]
        before = [getattr(i, key) for i in owners]
        payload = {key: _forged(owners[0], key)}
        await tool_cls(AsyncMock(), AsyncMock())._update_appeal_fields(
            appeal, denial, payload
        )
        assert [getattr(i, key) for i in owners] == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _NEVER_SETTABLE_PRIOR_AUTH)
    async def test_prior_auth_payload_never_sets_status_or_bookkeeping(self, key):
        prior_auth = PriorAuthRequest()
        before = getattr(prior_auth, key)
        await PriorAuthTool(AsyncMock(), AsyncMock())._update_prior_auth_fields(
            prior_auth, {key: "forged"}
        )
        assert getattr(prior_auth, key) == before

    @pytest.mark.asyncio
    async def test_letter_payload_leaves_the_appeals_letter_alone(self):
        """The letter comes from the pipeline, not the payload."""
        appeal = Appeal(appeal_text="Rebecca Lee Crumpler's own letter.")
        await GenerateAppealLetterTool(AsyncMock(), AsyncMock())._update_appeal_fields(
            appeal, Denial(), {"appeal_text": "A letter from the payload."}
        )
        assert appeal.appeal_text == "Rebecca Lee Crumpler's own letter."


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_new_appeal_rows_take_the_chats_hashed_email_not_the_payloads():
    """Deletion finds a chat's denial and appeal by the chat's hashed email,
    so a payload must not be able to swap it."""
    chat_hash = Denial.get_hashed_email("rebecca.crumpler@example.org")
    chat = await OngoingChat.objects.acreate(
        hashed_email=chat_hash, chat_history=[], summary_for_next_call=[]
    )
    await AppealTool(AsyncMock(), AsyncMock()).handle(
        '**create_or_update_appeal**{"procedure": "MRI", "hashed_email": "forged"}',
        "",
        chat=chat,
    )
    appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
    assert (appeal.hashed_email, appeal.for_denial.hashed_email) == (
        chat_hash,
        chat_hash,
    )


class TestToolFieldTextCoercion:
    """set_tool_field gives text columns text on the in-memory instance: the
    letter tool reads the denial it just updated (calling str methods on it)
    before anything refetches it."""

    def test_number_for_a_text_column_is_stringified(self):
        denial = Denial()
        set_tool_field(denial, "procedure", 72148)
        assert denial.procedure == "72148"

    def test_list_of_plain_values_is_joined(self):
        denial = Denial()
        set_tool_field(denial, "diagnosis", ["M54.5", "M51.26"])
        assert denial.diagnosis == "M54.5, M51.26"

    def test_nested_object_for_a_text_column_is_skipped(self):
        denial = Denial(procedure="MRI")
        set_tool_field(denial, "procedure", {"code": "72148"})
        assert denial.procedure == "MRI"

    def test_boolean_for_a_text_column_is_skipped(self):
        denial = Denial(diagnosis="back pain")
        set_tool_field(denial, "diagnosis", True)
        assert denial.diagnosis == "back pain"

    def test_boolean_column_keeps_a_real_boolean(self):
        """Stringifying it would make False truthy in memory."""
        denial = Denial(professional_to_finish=True)
        set_tool_field(denial, "professional_to_finish", False)
        assert denial.professional_to_finish is False

    def test_null_for_a_not_null_column_is_skipped(self):
        """A null denial_text would fail the save with an IntegrityError."""
        denial = Denial(denial_text="Your MRI claim was denied.")
        set_tool_field(denial, "denial_text", None)
        assert denial.denial_text == "Your MRI claim was denied."

    def test_null_leaves_a_stored_value_alone(self):
        """A model's null means it doesn't know the field, not "erase it":
        the procedure an earlier turn stored stays for the letter."""
        denial = Denial(procedure="MRI")
        set_tool_field(denial, "procedure", None)
        assert denial.procedure == "MRI"


class TestPriorAuthPayloadFields:
    @pytest.mark.asyncio
    async def test_null_keeps_a_required_field(self):
        """Prior-auth payloads get the same field handling: a null for a NOT
        NULL column would fail the save and lose the call."""
        tool = PriorAuthTool(AsyncMock(), AsyncMock())
        prior_auth = PriorAuthRequest(diagnosis="back pain")
        await tool._update_prior_auth_fields(prior_auth, {"diagnosis": None})
        assert prior_auth.diagnosis == "back pain"


class TestToolErrorFrames:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_cls,token,helper",
        [
            (AppealTool, "create_or_update_appeal", "_get_or_create_appeal"),
            (
                PriorAuthTool,
                "create_or_update_prior_auth",
                "_get_or_create_prior_auth",
            ),
        ],
    )
    async def test_processing_error_frame_omits_the_exception_text(
        self, tool_cls, token, helper
    ):
        """The exception text can quote a database row, PHI included."""
        status, error = AsyncMock(), AsyncMock()
        tool = tool_cls(status, error)
        with patch.object(
            tool, helper, AsyncMock(side_effect=RuntimeError("SENTINEL-frame-err"))
        ):
            await tool.handle(f'{token} {{"diagnosis": "x"}}', "", chat=MagicMock())
        frames = [
            str(call.args) for call in status.await_args_list + error.await_args_list
        ]
        assert [f for f in frames if "SENTINEL-frame-err" in f] == []


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

    @staticmethod
    def _talking_tool(status):
        class _TalkingTool(BaseTool):
            pattern = r"\*\*talk\*\*"
            name = "Talker"

            async def execute(self, match, response_text, context, **kwargs):
                await self.send_status_message("Processing...")
                return response_text, context

        return _TalkingTool(status)

    @pytest.mark.asyncio
    async def test_a_departed_client_gets_no_second_status_frame(self):
        """A status frame finding the client gone is not this tool failing:
        no tool-error status is tried into the same closed socket."""
        from fighthealthinsurance.client_gone import ClientGone

        status = AsyncMock(side_effect=ClientGone())
        await self._talking_tool(status).handle("Answer. **talk**", "ctx")
        assert status.await_count == 1

    @pytest.mark.asyncio
    async def test_a_departed_client_keeps_the_reply_for_replay(self):
        """The model's reply already exists when a tool runs, so a hangup
        found by the tool's status frame hands the reply back (tool syntax
        stripped) for the turn to persist, instead of raising and abandoning
        the turn with the reply discarded (review)."""
        from fighthealthinsurance.client_gone import ClientGone

        status = AsyncMock(side_effect=ClientGone())
        cleaned, context, handled = await self._talking_tool(status).handle(
            "Answer. **talk**", "ctx"
        )
        assert (cleaned, context, handled) == ("Answer.", "ctx", True)

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

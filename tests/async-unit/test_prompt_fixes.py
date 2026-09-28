"""Regressions for the prompt clean-up.

Each test pins one property of the text that reaches a model:

- context the appeal prompt already carries is not injected a second time
  through ``_build_context_extra``;
- the prior-auth prompt no longer sends a literal ``{{what_to_gen}}`` and
  honours the urgent flag;
- the regulator cover letter is written in the patient's voice unless the
  denial is being finished by a professional, with its own system prompt;
- the denial-letter summarizer uses a denial-letter prompt, not the
  PubMed-article one;
- the citation parser strips ``[1]``-style numbering;
- the appeal prompt is a sequence of labelled sections;
- the chat system prompt advertises one tool-call syntax per tool, states
  the panda-summary rule once, and attaches the Medicaid tool blocks iff
  Medicaid shows up anywhere in the chat.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.escalation_addresses import EscalationRecipient
from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.generate_prior_auth import PriorAuthGenerator
from fighthealthinsurance.generate_regulator_letter import (
    REGULATOR_LETTER_SYSTEM_PROMPT,
    generate_regulator_letter,
    make_regulator_letter_prompt,
)
from fighthealthinsurance.ml.ml_models import (
    PRIOR_AUTH_SYSTEM_PROMPT,
    RemoteFullOpenLike,
    RemoteModelLike,
    RemoteOpenLike,
)
from fighthealthinsurance.ml.ml_router import MLRouter


def _model(**kwargs) -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://primary.test/v1", "tok", "test-model", **kwargs)


# --- _build_context_extra ---------------------------------------------------


class TestContextExtraSkipsWhatThePromptCarries:
    def test_context_already_in_prompt_is_not_injected_again(self):
        m = _model()
        plan = "Plan documents: MRI covered after 6 weeks of conservative care."
        pubmed = "PMID 12345678: lumbar MRI outcomes."
        citation = "Smith et al., MRI timing, Spine, 2021"
        prompt = f"TASK: appeal\n\nPLAN DETAILS: {plan}\n\nPubMed references (use these): {pubmed}\n\nProvided citations (use these): {citation}"
        extra = m._build_context_extra(
            patient_context="patient has had 8 weeks of PT",
            pubmed_context=pubmed,
            plan_context=plan,
            ml_citations_context=[citation],
            prompt=prompt,
        )
        # Patient history is not in the prompt, so it is still injected...
        assert "8 weeks of PT" in extra
        # ...but the three the prompt already carries are not repeated.
        assert plan not in extra
        assert pubmed not in extra
        assert citation not in extra

    def test_context_absent_from_prompt_is_injected(self):
        m = _model()
        extra = m._build_context_extra(
            patient_context="history",
            pubmed_context="PMID 1",
            plan_context="plan text",
            ml_citations_context=["Cite A", "Cite B"],
            prompt="Write an appeal.",
        )
        assert "history" in extra
        assert "PMID 1" in extra
        assert "plan text" in extra
        assert "Cite A\nCite B" in extra

    def test_citation_list_is_rendered_as_lines_not_a_python_repr(self):
        m = _model()
        extra = m._build_context_extra(ml_citations_context=["Cite A", "Cite B"])
        assert "['Cite A', 'Cite B']" not in extra
        assert "Cite A\nCite B" in extra

    def test_empty_citation_list_adds_nothing(self):
        m = _model()
        assert m._build_context_extra(ml_citations_context=[]) == ""


# --- citation parser -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_citations_strips_bracketed_numbering():
    m = _model()
    reply = (
        "[1] Smith et al., MRI timing, Spine, 2021, https://doi.org/10.1/a\n"
        "[2]Jones et al., Conservative care, JAMA, 2020\n"
        "(3) Lee et al., Imaging guidelines, Radiology, 2019"
    )
    with patch.object(m, "get_system_prompts", return_value=["sys"]), patch.object(
        m, "_infer", new=AsyncMock(return_value=(reply, []))
    ):
        citations = await m.get_citations(
            denial_text="denied", procedure="MRI", diagnosis="back pain"
        )
    assert citations == [
        "Smith et al., MRI timing, Spine, 2021, https://doi.org/10.1/a",
        "Jones et al., Conservative care, JAMA, 2020",
        "Lee et al., Imaging guidelines, Radiology, 2019",
    ]


# --- prior auth prompt -----------------------------------------------------


def _pa_context(**overrides):
    base = {
        "diagnosis": "type 2 diabetes",
        "treatment": "Ozempic",
        "insurance_company": "Aetna",
        "patient_health_history": "",
        "provider_info": "Dr. Who, NPI 1234567890",
        "qa_pairs": {"Has the patient tried metformin?": "Yes, 6 months"},
        "urgent": False,
        "patient_info": {"name": "Pat Example", "dob": "1980-01-01"},
        "proposal_type": "letter",
        "rxnorm_hint": "",
    }
    base.update(overrides)
    return base


class TestPriorAuthPrompt:
    @pytest.mark.asyncio
    async def test_no_literal_template_braces_reach_the_model(self):
        prompt = await PriorAuthGenerator()._create_prompt(_pa_context(), letter=True)
        assert "{{what_to_gen}}" not in prompt
        assert "{what_to_gen}" not in prompt
        assert "formal prior authorization request letter with:" in prompt

    @pytest.mark.asyncio
    async def test_urgent_flag_is_stated(self):
        urgent = await PriorAuthGenerator()._create_prompt(
            _pa_context(urgent=True), letter=True
        )
        routine = await PriorAuthGenerator()._create_prompt(
            _pa_context(urgent=False), letter=True
        )
        assert "URGENT" in urgent and "expedited review" in urgent
        assert "URGENT" not in routine

    @pytest.mark.asyncio
    async def test_intake_data_is_rendered_as_lines_not_dict_reprs(self):
        prompt = await PriorAuthGenerator()._create_prompt(_pa_context(), letter=True)
        assert "- Has the patient tried metformin?: Yes, 6 months" in prompt
        assert "- name: Pat Example" in prompt
        assert "{'name'" not in prompt
        assert "{'Has the patient" not in prompt

    @pytest.mark.asyncio
    async def test_unknown_treatment_does_not_render_as_none(self):
        prompt = await PriorAuthGenerator()._create_prompt(
            _pa_context(treatment=None, diagnosis=None), letter=True
        )
        assert "None" not in prompt
        assert "the requested treatment" in prompt


# --- regulator letter ------------------------------------------------------


def _denial(**overrides):
    base = dict(
        insurance_company="Aetna",
        procedure="MRI",
        diagnosis="back pain",
        claim_id="C-1",
        plan_id="",
        qa_context="",
        denial_text="Your MRI was denied as not medically necessary.",
        professional_to_finish=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _recipient(recipient_type="doi", **extra):
    return EscalationRecipient(
        recipient_type=recipient_type,
        name="Some Regulator",
        extra=extra,
    )


class TestRegulatorLetterPrompt:
    def test_default_voice_is_the_patient(self):
        prompt = make_regulator_letter_prompt(_denial(), _recipient())
        assert "from the patient, writing in the first person" in prompt
        assert "treating healthcare professional" not in prompt

    def test_professional_to_finish_switches_voice(self):
        prompt = make_regulator_letter_prompt(
            _denial(professional_to_finish=True), _recipient()
        )
        assert "from the treating healthcare professional" in prompt
        assert "{{Your Practice}}" in prompt

    def test_explicit_professional_flag_wins(self):
        prompt = make_regulator_letter_prompt(
            _denial(professional_to_finish=True), _recipient(), professional=False
        )
        assert "from the patient, writing in the first person" in prompt

    def test_erisa_citation_only_offered_for_the_ebsa_letter(self):
        doi = make_regulator_letter_prompt(_denial(), _recipient("doi"))
        ebsa = make_regulator_letter_prompt(_denial(), _recipient("dol_ebsa"))
        assert "29 C.F.R." not in doi
        assert "29 C.F.R. § 2560.503-1" in ebsa

    def test_ask_and_role_are_recipient_specific(self):
        doi = make_regulator_letter_prompt(_denial(), _recipient("doi"))
        md = make_regulator_letter_prompt(_denial(), _recipient("medical_director"))
        assert "Recipient role: state insurance regulator" in doi
        assert "Recipient role: doi" not in doi
        assert "End with one concrete ask: an investigation" in doi
        assert "End with one concrete ask: a peer-to-peer review" in md
        assert "EBSA inquiry" not in doi

    def test_long_denial_text_is_cut_at_a_boundary(self):
        text = "First sentence about the denial. " * 400
        prompt = make_regulator_letter_prompt(_denial(denial_text=text), _recipient())
        excerpt = prompt.split("Denial letter excerpt:\n", 1)[1].split(
            "\n\nToday's date", 1
        )[0]
        assert len(excerpt) <= 4000 + 10
        assert excerpt.rstrip().endswith((".", "…"))

    @pytest.mark.asyncio
    async def test_generation_passes_its_own_system_prompt_and_pov(self):
        model = AsyncMock(spec=RemoteModelLike)
        model.generate_prior_auth_response.return_value = "Dear Regulator, " * 10
        with patch(
            "fighthealthinsurance.generate_regulator_letter.ml_router.get_chat_backends",
            return_value=[model],
        ):
            text = await generate_regulator_letter(
                _denial(professional_to_finish=False), _recipient()
            )
        assert text is not None
        kwargs = model.generate_prior_auth_response.call_args.kwargs
        assert kwargs["system_prompt"] == REGULATOR_LETTER_SYSTEM_PROMPT
        assert kwargs["system_prompt"] != PRIOR_AUTH_SYSTEM_PROMPT
        assert kwargs["prof_pov"] is False


# --- denial-letter summarizer ---------------------------------------------


class TestSummarizeDenialLetter:
    @pytest.mark.asyncio
    async def test_uses_a_denial_letter_prompt_not_the_article_one(self):
        router = MLRouter()
        internal = AsyncMock(spec=RemoteModelLike)
        internal._infer_no_context.return_value = "condensed denial summary"
        router.internal_models_by_cost = [internal]

        result = await router.summarize_denial_letter(
            "Aetna denied CPT 72148 on 2026-01-02.",
            use_external=False,
            max_input_chars=1000,
        )

        assert result == "condensed denial summary"
        kwargs = internal._infer_no_context.call_args.kwargs
        prompt = kwargs["prompt"]
        assert "denial letter" in prompt
        assert "CPT/HCPCS/NDC" in prompt
        assert "appeal deadline" in prompt
        assert "article" not in prompt
        assert "US studies" not in prompt
        assert "Aetna denied CPT 72148" in prompt
        assert kwargs["system_prompts"] == [MLRouter.DENIAL_SUMMARY_SYSTEM_PROMPT]

    @pytest.mark.asyncio
    async def test_truncation_is_marked(self):
        router = MLRouter()
        internal = AsyncMock(spec=RemoteModelLike)
        internal._infer_no_context.return_value = "condensed denial summary"
        router.internal_models_by_cost = [internal]

        await router.summarize_denial_letter(
            "B" * 500, use_external=False, max_input_chars=100
        )
        prompt = internal._infer_no_context.call_args.kwargs["prompt"]
        assert "B" * 100 in prompt
        assert "B" * 101 not in prompt
        assert "cut off here" in prompt

    @pytest.mark.asyncio
    async def test_blank_text_calls_no_model(self):
        router = MLRouter()
        internal = AsyncMock(spec=RemoteModelLike)
        router.internal_models_by_cost = [internal]
        assert (
            await router.summarize_denial_letter(
                "   ", use_external=False, max_input_chars=100
            )
            is None
        )
        internal._infer_no_context.assert_not_called()


# --- appeal prompt structure -----------------------------------------------


class TestMakeOpenPromptStructure:
    def _prompt(self, **overrides):
        kwargs = dict(
            denial_text="Your MRI was denied as not medically necessary.",
            procedure="MRI",
            diagnosis="back pain",
        )
        kwargs.update(overrides)
        prompt = AppealGenerator().make_open_prompt(**kwargs)
        assert prompt is not None
        return prompt

    def test_task_first_and_denial_letter_last(self):
        prompt = self._prompt()
        assert prompt.startswith("TASK: Write a health insurance appeal")
        assert prompt.rstrip().endswith(
            "DENIAL LETTER:\nYour MRI was denied as not medically necessary."
        )

    def test_identifiers_are_a_labelled_list(self):
        prompt = self._prompt(
            plan_id="P-1", claim_id="C-9", insurance_company="Aetna", qa_context="Q/A"
        )
        details = prompt.split("DETAILS TO INCLUDE", 1)[1].split("\n\n", 1)[0]
        assert "- Insurance company: Aetna" in details
        assert "- Plan ID: P-1" in details
        assert "- Claim ID: C-9" in details
        assert "intake questions" in details and "Q/A" in details

    def test_unknown_identifiers_are_omitted(self):
        prompt = self._prompt(plan_id="UNKNOWN", claim_id="", insurance_company=None)
        assert "DETAILS TO INCLUDE" not in prompt
        assert "UNKNOWN" not in prompt

    def test_no_sentence_fragment_artifacts(self):
        prompt = self._prompt(plan_id="P-1", qa_context="Q/A", is_trans=True)
        assert "\n. " not in prompt
        assert ". ." not in prompt
        assert "the patient is trans" in prompt

    def test_professional_pov_section_carries_the_sign_off(self):
        prompt = self._prompt(professional_to_finish=True, professional="Dr. Who")
        assert "POINT OF VIEW: Write as the treating healthcare professional" in prompt
        assert "Sign the letter as Dr. Who." in prompt
        # The examples live in the system prompt now, not here.
        assert "GOOD EXAMPLE" not in prompt

    def test_patient_pov_has_no_pov_section(self):
        assert "POINT OF VIEW" not in self._prompt()

    def test_plan_context_appears_once(self):
        plan = "Plan covers MRI after conservative care."
        prompt = self._prompt(plan_context=plan)
        assert prompt.count(plan) == 1
        assert "PLAN DETAILS:" in prompt

    def test_sections_are_blank_line_separated(self):
        prompt = self._prompt(pubmed_context="PMID 1", plan_id="P-1")
        for label in (
            "TASK:",
            "DETAILS TO INCLUDE",
            "CITATION INSTRUCTIONS",
            "DENIAL LETTER:",
        ):
            assert f"\n\n{label}" in prompt or prompt.startswith(label)


# --- chat system prompt ----------------------------------------------------


class RecordingChat(RemoteOpenLike):
    """RemoteOpenLike whose _infer records the system prompt it was given."""

    def __init__(self):
        super().__init__(
            api_base="http://example.invalid",
            token="test-token",
            model="test-model",
            system_prompts_map={},
        )
        self.system_prompts: list[str] = []

    async def _infer(self, system_prompts, prompt, **kwargs):
        self.system_prompts.extend(system_prompts)
        return ("Sure, tell me more.🐼 Appeal chat.", None)


async def _chat_system_prompt(message: str, history=None) -> str:
    model = RecordingChat()
    await model.generate_chat_response(
        message, history=history, is_professional=False, is_logged_in=False
    )
    assert len(model.system_prompts) == 1
    return model.system_prompts[0]


class TestChatSystemPrompt:
    @pytest.mark.asyncio
    async def test_one_syntax_per_tool(self):
        sp = await _chat_system_prompt("How do I appeal an MRI denial?")
        assert "**pubmed_query:" in sp
        assert "pubmedquery" not in sp
        assert "[*pubmed query" not in sp
        assert "**clinical_trials_query:" in sp
        assert "[*clinical trials query" not in sp

    @pytest.mark.asyncio
    async def test_panda_summary_rule_stated_once(self):
        sp = await _chat_system_prompt("How do I appeal an MRI denial?")
        assert sp.count("At the end of every response") == 1

    @pytest.mark.asyncio
    async def test_medicaid_tools_absent_without_medicaid_in_the_chat(self):
        sp = await _chat_system_prompt("How do I appeal an MRI denial?")
        assert "**medicaid_eligibility" not in sp
        assert "**medicaid_info" not in sp

    @pytest.mark.asyncio
    async def test_medicaid_tools_attached_when_medicaid_is_anywhere_in_history(self):
        history = [{"role": "user", "content": "Does Medicaid cover this?"}]
        # Well past the old 40-message window.
        for i in range(60):
            history.append({"role": "user", "content": f"unrelated turn {i}"})
            history.append({"role": "assistant", "content": f"reply {i}🐼"})
        sp = await _chat_system_prompt("What else do I need?", history=history)
        assert "**medicaid_eligibility" in sp
        assert "**medicaid_info" in sp


def test_medicaid_detection_scans_the_whole_history():
    model = RecordingChat()
    history = [{"role": "user", "content": "medi-cal question"}] + [
        {"role": "user", "content": f"turn {i}"} for i in range(80)
    ]
    assert model._is_medicaid_related("ok", None, history) is True
    assert model._is_medicaid_related("ok", None, history[1:]) is False

import io
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from loguru import logger as loguru_logger
from fighthealthinsurance.ml.ml_models import (
    DeadlineSkipped,
    ProviderUnavailable,
    RemoteFullOpenLike,
)
from fighthealthinsurance.ml.model_attempt_log import ModelAttemptRecorder
from fighthealthinsurance.generate_appeal import (
    backend_label,
    AppealGenerator,
    AppealTemplateGenerator,
    ExtractionUnavailable,
    GeneratedAppeal,
    MentalHealthParityAppeal,
    _add_proactive_shed_variants,
    _calls_worth_shedding,
    _generated_to_appeals_text,
    _calls_over_context_budget,
    _DUAL_CALL_CONTEXT_RATIO,
    _estimate_call_token_footprint,
    _model_context_limit,
    _peek_real_or_none,
    _shed_context,
    _summarize_model_outcomes,
    _PROMPT_TIER1_NULLS,
    _PROMPT_TIER1_STRIP_GATED,
    _PROMPT_TIER2_TRUNCATIONS,
    _SHEDDABLE_TIER1,
    _TIER2_TRUNCATIONS,
)


def _ga(text):
    return GeneratedAppeal(text=text, model_name="test-model")


class TestAppealQuestionsGeneration:
    """Tests for the question generation functionality in RemoteFullOpenLike."""

    @pytest.fixture(autouse=True)
    def setup(self):
        # Create a mock RemoteFullOpenLike instance
        self.model = MagicMock(spec=RemoteFullOpenLike)
        # Set up _infer_no_context as AsyncMock
        self.model._infer_no_context = AsyncMock()
        # Set get_system_prompts to return a test prompt
        self.model.get_system_prompts = MagicMock(return_value=["Test system prompt"])
        # Add model attribute used in logging (line 1908 in ml_models.py)
        self.model.model = "test-model"

    @pytest.mark.asyncio
    async def test_get_appeal_questions_basic(self):
        """Test basic question generation with different response formats."""
        # Mock the _infer_no_context response for a simple formatted output
        self.model._infer_no_context.return_value = """
        1. What medical evidence supports the necessity of this treatment? Clinical studies show efficacy
        2. Has the patient tried alternative treatments? No alternatives attempted
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        assert len(result) == 2
        assert (
            result[0][0]
            == "What medical evidence supports the necessity of this treatment?"
        )
        assert result[0][1] == "Clinical studies show efficacy"
        assert result[1][0] == "Has the patient tried alternative treatments?"
        assert result[1][1] == "No alternatives attempted"

    @pytest.mark.asyncio
    async def test_get_appeal_questions_markdown_format(self):
        """Test question generation with markdown formatted output."""
        # Mock the _infer_no_context response for markdown formatted output
        self.model._infer_no_context.return_value = """
        **What medical evidence supports the necessity of this treatment?** Clinical studies show efficacy
        **Has the patient tried alternative treatments?** No alternatives attempted
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        assert len(result) == 2
        assert (
            result[0][0]
            == "What medical evidence supports the necessity of this treatment?"
        )
        assert result[0][1] == "Clinical studies show efficacy"
        assert result[1][0] == "Has the patient tried alternative treatments?"
        assert result[1][1] == "No alternatives attempted"

    @pytest.mark.asyncio
    async def test_get_appeal_questions_multi_questions_per_line(self):
        """Test question generation with multiple questions per line.

        Note: The implementation uses split("?", 1) which only splits on the first
        question mark. Multiple questions on one line are NOT split - the answer
        contains everything after the first "?".
        """
        # Mock the _infer_no_context response with multiple questions per line
        self.model._infer_no_context.return_value = """
        Was the stroke confirmed to occur during birth? Yes. Was it localized to the left MCA? Yes, it was.
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Implementation uses split("?", 1) so only first question is extracted
        # Everything after the first "?" becomes the answer
        assert len(result) == 1
        assert result[0][0] == "Was the stroke confirmed to occur during birth?"
        # The rest of the line holds a second question, so it is not shown
        # as a hint: a hint is an answer, not more questions.
        assert result[0][1] == ""

    @pytest.mark.asyncio
    async def test_get_appeal_questions_no_question_mark(self):
        """Test question generation with text without question marks."""
        # Mock the _infer_no_context response with no question marks
        self.model._infer_no_context.return_value = """
        This treatment is necessary
        Patient history includes condition X
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # A line is a question only when the model wrote a question mark.
        # Adding one to every other line is how a model's refusal ("I cannot
        # generate specific clinical questions...") was shown as a question.
        assert result is None

    @pytest.mark.asyncio
    async def test_get_appeal_questions_empty_response(self):
        """Test question generation with an empty response."""
        # Mock the _infer_no_context response with None
        self.model._infer_no_context.return_value = None

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # No reply is not "nothing to ask": [] is kept for NO_QUESTIONS.
        assert result is None

    @pytest.mark.asyncio
    async def test_get_appeal_questions_rationale_format(self):
        """Test handling of 'Rationale for questions' in response."""
        # Mock the _infer_no_context response with 'Rationale for questions'
        self.model._infer_no_context.return_value = """
        Rationale for questions: These questions will help establish medical necessity.

        1. What is the patient's age?
        2. Has the patient tried conservative treatments?
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Responses with "Rationale for questions" are rejected: no usable reply.
        assert result is None

    @pytest.mark.asyncio
    async def test_get_appeal_questions_with_answer_prefix(self):
        """Test parsing questions with answer prefixes like 'A:' or ':'."""
        # Mock the _infer_no_context response
        self.model._infer_no_context.return_value = """
        What is the diagnosis code? A: J84.112
        Is this treatment FDA approved?: Yes it is
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        assert len(result) == 2
        assert result[0][0] == "What is the diagnosis code?"
        assert result[0][1] == "J84.112"
        assert result[1][0] == "Is this treatment FDA approved?"
        assert result[1][1] == "Yes it is"

    @pytest.mark.asyncio
    async def test_get_appeal_questions_answers_starting_with_a_keep_first_letter(
        self,
    ):
        """The 'A:' prefix stripper must not eat a bare leading 'A': the old
        [A:] character class turned 'Age 47' into 'ge 47' and 'Atorvastatin'
        into 'torvastatin', corrupting answers on their way into qa_context
        and from there into the appeal prompt."""
        self.model._infer_no_context.return_value = """
        What is the patient's age? Age 47
        What medication was prescribed? Atorvastatin 40mg daily
        """

        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        assert len(result) == 2
        assert result[0][1] == "Age 47"
        assert result[1][1] == "Atorvastatin 40mg daily"


# --- Shared fixtures for make_appeals tests ----------------------------------


def _make_call(**overrides):
    """Build a `calls`-shape dict matching make_appeals' actual 8-key
    schema. The uspstf/pa/nice/rag contexts are NOT call-dict keys —
    they're inlined into `prompt` by make_open_prompt before calls is
    built — so they belong in the prompt string, not as separate keys."""
    base = {
        "model_name": "fhi-internal",
        "prompt": "Please write an appeal.",
        "patient_context": "patient medical history",
        "plan_context": "plan documents summary",
        "infer_type": "full",
        "pubmed_context": "pubmed citations",
        "ml_citations_context": ["citation-1", "citation-2"],
        "prof_pov": False,
    }
    base.update(overrides)
    return base


def _mock_denial(use_external=False, denial_id=42):
    denial = MagicMock()
    denial.denial_id = denial_id
    denial.use_external = use_external
    for attr in (
        "qa_context",
        "health_history",
        "plan_context",
        "plan_documents_summary",
        "claim_id",
    ):
        setattr(denial, attr, None)
    denial.professional_to_finish = False
    denial.diagnosis = "dx"
    denial.procedure = "px"
    denial.denial_text = "denial"
    denial.insurance_company = "ins"
    return denial


def _drain_make_appeals(denial, generate_names, models_by_name):
    """Drive make_appeals through the full failure path. Returns nothing —
    callers spy on side effects (router calls or loguru output)."""
    gen = AppealGenerator()
    tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=generate_names,
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new=models_by_name,
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        try:
            list(
                gen.make_appeals(
                    denial,
                    tmpl,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                )
            )
        except Exception:
            pass


@contextmanager
def _loguru_capture(level="WARNING"):
    sink = io.StringIO()
    handler_id = loguru_logger.add(sink, level=level)
    try:
        yield sink
    finally:
        loguru_logger.remove(handler_id)


def _name_spy():
    """Return (spy_fn, calls_list) for ml_router.generate_text_backend_names.
    Calls list records each `use_external` value passed in."""
    calls: list[bool] = []

    def spy(use_external=False, fail_open=True):
        calls.append(use_external)
        return ["nonexistent-model"]

    return spy, calls


# --- _shed_context unit tests -----------------------------------------------


class TestShedContext:
    """_shed_context drops/truncates context in priority order for retry."""

    def test_tier1_drops_pubmed_and_citations(self):
        new_calls, changed = _shed_context([_make_call()], tier=1)
        new = new_calls[0]
        for key in _SHEDDABLE_TIER1:
            assert new[key] is None
        # Core context never dropped by tier 1
        assert new["plan_context"] == "plan documents summary"
        assert new["patient_context"] == "patient medical history"
        assert set(changed) == set(_SHEDDABLE_TIER1)

    def test_tier2_truncates_core_when_over_cap(self):
        oversized = {key: "X" * (cap + 100) for key, cap in _TIER2_TRUNCATIONS}
        new_calls, changed = _shed_context([_make_call(**oversized)], tier=2)
        new = new_calls[0]
        # Tier 2 also nulls tier-1 keys
        for key in _SHEDDABLE_TIER1:
            assert new[key] is None
        for key, cap in _TIER2_TRUNCATIONS:
            assert len(new[key]) == cap
            assert f"{key}(truncated)" in changed

    def test_tier2_skips_truncation_under_cap(self):
        new_calls, changed = _shed_context(
            [_make_call(plan_context="short", patient_context="also short")],
            tier=2,
        )
        new = new_calls[0]
        assert new["plan_context"] == "short"
        assert new["patient_context"] == "also short"
        assert not any("(truncated)" in c for c in changed)

    def test_tier2_call_dict_truncation_is_boundary_aware(self):
        # Regression: the call-dict surface previously hard-cut val[:cap]
        # mid-word, diverging from the boundary-aware prompt surface even
        # though plan_context carries the same value on both. Both now use
        # truncate_at_boundary, so the call-dict copy must end on a word
        # boundary (not a partial token) and stay within the cap.
        (key, cap), *_ = _TIER2_TRUNCATIONS
        oversized = "word " * (cap // 2)  # spaces give a boundary to cut on
        new_calls, _ = _shed_context([_make_call(**{key: oversized})], tier=2)
        result = new_calls[0][key]
        assert len(result) <= cap
        # Boundary-aware: result is a prefix ending on a word boundary, so it
        # does not end mid-token and remains a prefix of the input.
        assert not result.endswith("wor")
        assert oversized.startswith(result.rstrip())

    def test_does_not_mutate_input_calls(self):
        original = _make_call()
        _shed_context([original], tier=2)
        assert original["pubmed_context"] == "pubmed citations"
        assert original["ml_citations_context"] == ["citation-1", "citation-2"]

    def test_already_none_inputs_not_reported_as_changed(self):
        _, changed = _shed_context(
            [_make_call(pubmed_context=None, plan_context=None)], tier=2
        )
        assert "pubmed_context" not in changed

    def test_tier0_is_a_noop(self):
        # Regression (CodeRabbit on PR #824): _shed_context's documented
        # tier contract is "tier N applies all tier-<=N reductions". Tier 0
        # must therefore shed nothing — neither the prompt surface (already
        # guarded on tier >= 1) nor the call-dict surface.
        original = _make_call()
        new_calls, changed = _shed_context([original], tier=0)
        assert changed == []
        # Tier-1 call-dict keys survive unchanged.
        for key in _SHEDDABLE_TIER1:
            assert new_calls[0][key] == original[key]
        # Tier-2 call-dict keys also survive unchanged (no truncation).
        for key, _cap in _TIER2_TRUNCATIONS:
            assert new_calls[0][key] == original[key]

    def test_tier1_stamps_context_level(self):
        # Each shed call records the shed level so the produced appeal's
        # ProposedAppeal row can persist its provenance.
        new_calls, _ = _shed_context([_make_call()], tier=1)
        assert new_calls[0]["context_level"] == "tier1_shed"

    def test_tier2_stamps_context_level(self):
        new_calls, _ = _shed_context([_make_call()], tier=2)
        assert new_calls[0]["context_level"] == "tier2_shed"

    def test_tier0_does_not_stamp_context_level(self):
        # Tier 0 is a no-op and must not fabricate a shed level (the call keeps
        # its inherited "full" level, which _make_call doesn't set).
        new_calls, _ = _shed_context([_make_call()], tier=0)
        assert "context_level" not in new_calls[0]


# --- _shed_context prompt-rebuild tests -------------------------------------


def _prompt_kwargs(**overrides):
    """Build an ``open_prompt_kwargs`` shape matching make_appeals' actual
    call to make_open_prompt. Enrichment fields default to non-empty so
    tier-1 nulling has something to drop."""
    base = {
        "denial_text": "denial body",
        "procedure": "px",
        "diagnosis": "dx",
        "patient": None,
        "professional": None,
        "qa_context": None,
        "professional_to_finish": False,
        "plan_id": None,
        "claim_id": None,
        "insurance_company": "ins",
        "is_tpa": False,
        "ml_context": "ml ctx",
        "pubmed_context": "pubmed ctx",
        "plan_context": "patient plan body",
        "rag_context": "rag ctx",
        "nice_context": "nice ctx",
        "ucr_context": "ucr ctx",
        "payer_policy_context": "payer policy ctx",
        "pa_context": "pa ctx",
        "uspstf_context": "uspstf ctx",
        "clinical_trials_context": "clinical trials ctx",
        "regulatory_citation_context": "regulatory ctx",
        "medication_context": "med ctx",
    }
    base.update(overrides)
    return base


def _rebuild_spy(return_value="REBUILT"):
    """Return (seen_kwargs, rebuild_fn) for capturing _shed_context's
    rebuild_prompt invocation. Shared by the prompt-rebuild tests below so
    each one doesn't redefine the same 3-line closure."""
    seen_kwargs: dict = {}

    def rebuild(**rk):
        seen_kwargs.update(rk)
        return return_value

    return seen_kwargs, rebuild


def _rebuild_counter():
    """Return (count_box, rebuild_fn) for tests that only care whether
    rebuild_prompt was called, not what kwargs it saw. ``count_box[0]``
    holds the call count."""
    count_box = [0]

    def rebuild(**_):
        count_box[0] += 1
        return "REBUILT"

    return count_box, rebuild


class TestShedContextPromptRebuild:
    """Tier shedding must re-render the prompt with enrichment stripped.

    Without this, pubmed/citations/rag/nice/uspstf/etc. stay baked into
    ``open_prompt`` even after the call-dict copies are nulled, so the
    retry token count doesn't actually drop. Regression cover for
    PR #811 follow-up."""

    def test_tier1_rebuilds_prompt_with_enrichment_nulled(self):
        kwargs = _prompt_kwargs()
        seen_kwargs, rebuild = _rebuild_spy(return_value="REBUILT-PROMPT")
        new_calls, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=1,
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        # Every enrichment kwarg listed in _PROMPT_TIER1_NULLS gets nulled.
        for key in _PROMPT_TIER1_NULLS:
            assert seen_kwargs[key] is None, f"{key} not nulled in rebuild kwargs"
        # Non-enrichment kwargs survive.
        assert seen_kwargs["denial_text"] == "denial body"
        assert seen_kwargs["plan_context"] == "patient plan body"
        # Each nulled kwarg shows up in the changed list as prompt.<name>.
        for key in _PROMPT_TIER1_NULLS:
            assert f"prompt.{key}" in changed
        # Call's prompt was swapped to the rebuilt one.
        assert new_calls[0]["prompt"] == "REBUILT-PROMPT"

    def test_tier1_sheds_clinical_trials_context(self):
        # Regression: clinical_trials_context (added to make_open_prompt in
        # #821) is a prompt-baked enrichment with no call-dict copy, so it
        # must be in _PROMPT_TIER1_NULLS or a context-overflow retry would
        # leave it pinning the token count — the exact bug this PR fixed for
        # pubmed/citations.
        assert "clinical_trials_context" in _PROMPT_TIER1_NULLS
        seen_kwargs, rebuild = _rebuild_spy()
        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(clinical_trials_context="NCT0123 ..."),
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert seen_kwargs["clinical_trials_context"] is None
        assert "prompt.clinical_trials_context" in changed

    def test_tier1_sheds_regulatory_citation_context(self):
        # Regression: regulatory_citation_context (added to make_open_prompt
        # in #834) is a prompt-baked enrichment with no call-dict copy, so
        # it must be in _PROMPT_TIER1_NULLS or a context-overflow retry would
        # leave it pinning the token count.
        assert "regulatory_citation_context" in _PROMPT_TIER1_NULLS
        seen_kwargs, rebuild = _rebuild_spy()
        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(
                regulatory_citation_context="42 U.S.C. ..."
            ),
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert seen_kwargs["regulatory_citation_context"] is None
        assert "prompt.regulatory_citation_context" in changed

    def test_tier1_preserves_specialized_hint_suffix(self):
        # Specialized calls use `open_prompt + "\n\n--- ... ---\n" + hint`.
        # The shed pass must swap the prefix while keeping the suffix so the
        # specialized template hint isn't lost on retry.
        suffix = "\n\n--- Denial-type guidance ---\nMHPAEA hint"
        new_calls, _ = _shed_context(
            [_make_call(prompt="ORIGINAL" + suffix)],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(),
            rebuild_prompt=lambda **_: "SHED",
            original_open_prompt="ORIGINAL",
        )
        assert new_calls[0]["prompt"] == "SHED" + suffix

    def test_tier1_keeps_the_v2_contract_and_the_calls_prompt_version(self):
        # A v2 call's prompt ends with the output contract; shedding swaps the
        # front of the prompt and must keep both the contract and the version
        # the call drew, or a retried letter would be stored as the wrong one.
        from fighthealthinsurance.ml.appeal_prompt_versions import (
            OUTPUT_CONTRACT,
            PROMPT_V2,
            apply_prompt_version,
        )

        new_calls, _ = _shed_context(
            [
                _make_call(
                    prompt=apply_prompt_version("ORIGINAL", PROMPT_V2),
                    prompt_version=PROMPT_V2,
                )
            ],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(),
            rebuild_prompt=lambda **_: "SHED",
            original_open_prompt="ORIGINAL",
        )
        assert new_calls[0]["prompt"] == "SHED\n\n" + OUTPUT_CONTRACT
        assert new_calls[0]["prompt_version"] == PROMPT_V2

    def test_tier1_rerenders_each_layout_into_the_calls_built_from_it(self):
        # In thirds a run holds calls written with the original prompt (v1,
        # v2) and with the sectioned one (v3). Each layout is re-rendered by
        # its own builder from the same shed kwargs and swapped into the calls
        # that start with it, keeping the tail and the call's version.
        from fighthealthinsurance.ml.appeal_prompt_versions import (
            OUTPUT_CONTRACT,
            PROMPT_V1,
            PROMPT_V3,
            apply_prompt_version,
        )

        seen_original, rebuild_original = _rebuild_spy(return_value="SHED")
        seen_sectioned, rebuild_sectioned = _rebuild_spy(
            return_value="TASK: SHED SECTIONED"
        )
        new_calls, _ = _shed_context(
            [
                _make_call(prompt="ORIGINAL", prompt_version=PROMPT_V1),
                _make_call(
                    prompt=apply_prompt_version("TASK: SECTIONED", PROMPT_V3),
                    prompt_version=PROMPT_V3,
                ),
                _make_call(prompt="totally different med-necessary prompt"),
            ],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(),
            rebuild_prompt=rebuild_original,
            original_open_prompt="ORIGINAL",
            other_open_prompts=[("TASK: SECTIONED", rebuild_sectioned)],
        )
        assert [c["prompt"] for c in new_calls] == [
            "SHED",
            "TASK: SHED SECTIONED\n\n" + OUTPUT_CONTRACT,
            "totally different med-necessary prompt",
        ]
        assert [c.get("prompt_version") for c in new_calls] == [
            PROMPT_V1,
            PROMPT_V3,
            None,
        ]
        # Both builders saw the same shed kwargs.
        assert seen_sectioned == seen_original
        assert seen_sectioned["pubmed_context"] is None

    def test_tier1_leaves_unrelated_prompts_alone(self):
        # The medically-necessary prompt is a separate string and must not
        # be touched by the prefix swap.
        new_calls, _ = _shed_context(
            [_make_call(prompt="totally different med-necessary prompt")],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(),
            rebuild_prompt=lambda **_: "SHED",
            original_open_prompt="ORIGINAL",
        )
        assert new_calls[0]["prompt"] == "totally different med-necessary prompt"

    def test_tier2_truncates_in_prompt_plan_context_via_boundary(self):
        # Tier 2 truncates plan_context in the rebuilt prompt's kwargs as
        # well as in the call dict. Use a value past the cap so truncation
        # actually fires.
        (key, cap), *_ = _PROMPT_TIER2_TRUNCATIONS
        oversized = "para. " * (cap // 5)  # well past the cap
        kwargs = _prompt_kwargs(**{key: oversized})
        seen_kwargs, rebuild = _rebuild_spy(return_value="OUT")
        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=2,
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert isinstance(seen_kwargs[key], str)
        assert len(seen_kwargs[key]) <= cap
        assert f"prompt.{key}(truncated)" in changed

    def test_tier2_stacks_tier1_enrichment_nulls_in_prompt(self):
        # Stacking: tier 2 must also apply the tier-1 enrichment nulls to
        # the rebuilt prompt's kwargs, not just the call-dict copies.
        seen_kwargs, rebuild = _rebuild_spy(return_value="OUT")
        _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=2,
            open_prompt_kwargs=_prompt_kwargs(),
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        for key in _PROMPT_TIER1_NULLS:
            assert seen_kwargs[key] is None, f"{key} not nulled at tier 2"

    def test_omitted_rebuild_args_falls_back_to_call_dict_only(self):
        # When the caller doesn't pass rebuild args, _shed_context is a
        # pure call-dict shedder — same shape as the legacy tests above.
        new_calls, changed = _shed_context([_make_call()], tier=1)
        assert new_calls[0]["prompt"] == "Please write an appeal."  # unchanged
        assert not any(c.startswith("prompt.") for c in changed)

    def test_skips_rebuild_when_no_prompt_surface_changes(self):
        # Regression (Copilot review): make_open_prompt random.shuffle()s
        # the professional-POV example list, so re-calling it for a no-op
        # would silently change the retry prompt's example ordering even
        # though ``changed`` reports nothing shed. Skip the rebuild entirely
        # when no prompt kwarg was nulled or truncated.
        empty_kwargs = {key: None for key in _PROMPT_TIER1_NULLS}
        kwargs = _prompt_kwargs(**empty_kwargs, plan_context="short")
        calls_count, rebuild = _rebuild_counter()
        new_calls, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=2,  # tier 2 would otherwise try plan_context truncation
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert calls_count[0] == 0, "rebuild_prompt called despite no-op shed"
        assert not any(c.startswith("prompt.") for c in changed)
        # Call's prompt is untouched too (no swap happened).
        assert new_calls[0]["prompt"] == "ORIGINAL"

    def test_whitespace_only_enrichment_is_still_shed(self):
        # Regression (Copilot review on PR #824): for the ``!= ""``-gated
        # sections a whitespace-only value is NOT a no-op in make_open_prompt
        # --- ``"   "`` still trips has_citations and renders the CITATION
        # INSTRUCTIONS block plus the per-section header
        # (``Provided citations (use these):    ``). Those bytes need to drop
        # on retry, so the shed pass must null whitespace-only values and
        # trigger a rebuild. ``pa_context`` is ``.strip()``-gated and is
        # covered separately below, so it's excluded here.
        shed_keys = [
            k for k in _PROMPT_TIER1_NULLS if k not in _PROMPT_TIER1_STRIP_GATED
        ]
        whitespace_kwargs = {key: "   \n\t" for key in shed_keys}
        kwargs = _prompt_kwargs(**whitespace_kwargs)
        seen_kwargs, rebuild = _rebuild_spy(return_value="SHED")
        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=1,
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        # Every whitespace-only ``!= ""``-gated enrichment is nulled and reported.
        for key in shed_keys:
            assert seen_kwargs[key] is None, f"{key} should have been nulled"
            assert f"prompt.{key}" in changed

    def test_whitespace_only_pa_context_alone_skips_rebuild(self):
        # Regression (CodeRabbit on PR #844): pa_context is the one tier-1
        # section make_open_prompt gates on ``.strip()`` rather than ``!= ""``
        # (and it is not part of has_citations), so a whitespace-only
        # pa_context renders nothing there and is already a no-op. Nulling it
        # would force a rebuild that reshuffles make_open_prompt's randomized
        # GOOD EXAMPLEs and changes the retry prompt with zero size reduction.
        # With every other enrichment already empty, a whitespace-only
        # pa_context must NOT trigger a rebuild.
        assert "pa_context" in _PROMPT_TIER1_STRIP_GATED
        # Every other enrichment empty; only pa_context is whitespace.
        overrides = {key: "" for key in _PROMPT_TIER1_NULLS}
        overrides["pa_context"] = "   \n\t"
        kwargs = _prompt_kwargs(**overrides, plan_context="short")
        calls_count, rebuild = _rebuild_counter()
        new_calls, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=2,  # tier 2 would otherwise also try plan_context truncation
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert calls_count[0] == 0, "rebuild fired for whitespace-only pa_context"
        assert "prompt.pa_context" not in changed
        assert not any(c.startswith("prompt.") for c in changed)
        # No rebuild means the original prompt is reused untouched.
        assert new_calls[0]["prompt"] == "ORIGINAL"

    def test_truly_empty_string_enrichment_is_skipped(self):
        # The truly-empty string ``""`` IS already a no-op in
        # make_open_prompt (gate fails on ``!= ""``), so it can be skipped
        # without adding diagnostic noise to ``changed``. Pin that the
        # truly-empty case still avoids a rebuild call.
        empty_kwargs = {key: "" for key in _PROMPT_TIER1_NULLS}
        kwargs = _prompt_kwargs(**empty_kwargs, plan_context="short")
        calls_count, rebuild = _rebuild_counter()
        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=2,
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert calls_count[0] == 0, "rebuild fired on truly-empty enrichment"
        assert not any(c.startswith("prompt.") for c in changed)


# --- proactive dual-call (over-context-budget) tests ------------------------


def _backend_with_context(max_context):
    """A minimal model backend stub exposing get_max_context()."""
    backend = MagicMock()
    backend.get_max_context.return_value = max_context
    return backend


class TestDualCallContextBudget:
    """Proactive dual-call: over-budget calls get a tier-1 shed sibling on
    the first iteration (see make_appeals)."""

    def test_estimate_sums_prompt_and_context_surfaces(self):
        call = _make_call(
            prompt="p" * 40,
            patient_context="a" * 40,
            plan_context="b" * 40,
            pubmed_context="c" * 40,
            ml_citations_context="d" * 40,
        )
        # 5 fields x (40 // 4 chars-per-token) = 5 x 10 = 50.
        assert _estimate_call_token_footprint(call) == 50

    def test_estimate_handles_none_and_list_contexts(self):
        citations = ["x" * 20, "y" * 20]
        call = _make_call(
            prompt=None,
            patient_context=None,
            plan_context=None,
            pubmed_context=None,
            ml_citations_context=citations,
        )
        # Only the list contributes, counted the way the wire renders it:
        # one citation per line, not the list's Python repr.
        from fighthealthinsurance.context_utils import estimate_tokens

        assert _estimate_call_token_footprint(call) == estimate_tokens(
            "\n".join(citations)
        )

    def test_estimate_caps_patient_context_to_wire_size(self):
        # _build_context_extra only sends patient_context[0:max_len/2] chars,
        # so the estimate must count the capped size, not the full string.
        big_patient = "a" * 40000  # 10000 tokens uncapped
        call = _make_call(
            prompt=None,
            patient_context=big_patient,
            plan_context=None,
            pubmed_context=None,
            ml_citations_context=None,
        )
        # cap of 4000 chars -> 1000 tokens, far below the uncapped 10000.
        assert (
            _estimate_call_token_footprint(call, patient_context_char_cap=4000) == 1000
        )
        assert _estimate_call_token_footprint(call) == 10000

    def test_model_context_limit_uses_first_backend_in_routing_order(self):
        # get_model_result submits to backends in order and the first
        # successful submission serves the call, so the first backend's
        # window is the one that matters — not the smallest in the pool.
        models_by_name = {
            "fhi-internal": [
                _backend_with_context(100000),
                _backend_with_context(8000),
            ]
        }
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new=models_by_name,
        ):
            assert _model_context_limit("fhi-internal") == 100000

    def test_model_context_limit_falls_back_past_erroring_backend(self):
        bad = MagicMock()
        bad.get_max_context.side_effect = RuntimeError("boom")
        models_by_name = {"fhi-internal": [bad, _backend_with_context(8000)]}
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new=models_by_name,
        ):
            assert _model_context_limit("fhi-internal") == 8000

    def test_model_context_limit_missing_model_key_is_none(self):
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ):
            assert _model_context_limit("missing") is None

    def test_model_context_limit_none_model_name_is_none(self):
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ):
            assert _model_context_limit(None) is None

    def test_model_context_limit_survives_backend_error(self):
        bad = MagicMock()
        bad.get_max_context.side_effect = RuntimeError("boom")
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [bad]},
        ):
            # All backends erroring -> unknown, not a raised exception.
            assert _model_context_limit("fhi-internal") is None

    def test_over_budget_detects_calls_above_ratio(self):
        # ~8000 tokens of (sheddable) enrichment vs an 8000-token window
        # comfortably clears the 0.8*8000=6400 threshold.
        big = "x" * (8000 * 4)  # ~8000 tokens in one field
        call = _make_call(pubmed_context=big)
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(8000)]},
        ):
            over = _calls_over_context_budget([call])
        assert over == [call]

    def test_patient_context_alone_not_flagged_due_to_wire_cap(self):
        # A huge patient_context is capped to max_len/2 chars on the wire, so
        # it must NOT trip the over-budget check (tier-1 can't shed it anyway).
        big_patient = "x" * (8000 * 4)  # 8000 tokens uncapped
        call = _make_call(
            patient_context=big_patient, pubmed_context=None, ml_citations_context=None
        )
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(8000)]},
        ):
            # Capped to 4000 chars -> 1000 tokens, well under 6400.
            assert _calls_over_context_budget([call]) == []

    def test_under_budget_returns_empty(self):
        call = _make_call(prompt="short", patient_context="short", plan_context="short")
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(8000)]},
        ):
            assert _calls_over_context_budget([call]) == []

    def test_unknown_model_window_is_not_flagged(self):
        # No proactive shed when we can't size the window, even when the call
        # would otherwise be over budget.
        big = "x" * (8000 * 4)
        call = _make_call(pubmed_context=big)
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ):
            assert _calls_over_context_budget([call]) == []

    def test_ratio_is_a_sane_fraction(self):
        assert 0.0 < _DUAL_CALL_CONTEXT_RATIO < 1.0

    # --- _add_proactive_shed_variants integration (the make_appeals wiring) --

    @staticmethod
    def _noop_rebuild(**kwargs):
        # make_appeals passes self.make_open_prompt; tests inject this so the
        # prompt-rebuild path is exercised without a real prompt builder.
        return "REBUILT_PROMPT"

    def test_adds_shed_sibling_for_over_budget_call(self):
        big = "x" * (8000 * 4)
        call = _make_call(prompt="ORIGINAL", pubmed_context=big)
        original_snapshot = dict(call)
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(8000)]},
        ):
            result = _add_proactive_shed_variants(
                [call],
                open_prompt_kwargs={"pubmed_context": "enrichment"},
                rebuild_prompt=self._noop_rebuild,
                original_open_prompt="ORIGINAL",
                denial_id=1,
            )
        # Full call kept first and left untouched...
        assert result[0] is call
        assert call == original_snapshot
        # ...with a shed sibling appended that nulls the call-dict enrichment
        # and swaps in the rebuilt prompt.
        assert len(result) == 2
        sibling = result[1]
        assert sibling["pubmed_context"] is None
        assert sibling["ml_citations_context"] is None
        assert sibling["prompt"] == "REBUILT_PROMPT"

    def test_shed_sibling_of_a_v3_call_gets_the_rerendered_sectioned_prompt(self):
        big = "x" * (8000 * 4)
        call = _make_call(prompt="TASK: SECTIONED\n\nCONTRACT", pubmed_context=big)
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(8000)]},
        ):
            result = _add_proactive_shed_variants(
                [call],
                open_prompt_kwargs={"pubmed_context": "enrichment"},
                rebuild_prompt=self._noop_rebuild,
                original_open_prompt="ORIGINAL",
                other_open_prompts=[("TASK: SECTIONED", lambda **_: "TASK: SHED")],
                denial_id=1,
            )
        assert [c["prompt"] for c in result] == [
            "TASK: SECTIONED\n\nCONTRACT",
            "TASK: SHED\n\nCONTRACT",
        ]

    def test_no_sibling_when_under_budget(self):
        call = _make_call(prompt="ORIGINAL")
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(200000)]},
        ):
            result = _add_proactive_shed_variants(
                [call],
                open_prompt_kwargs={},
                rebuild_prompt=self._noop_rebuild,
                original_open_prompt="ORIGINAL",
                denial_id=1,
            )
        assert result == [call]

    def test_no_sibling_when_tier1_shed_is_noop(self):
        # Over budget purely from plan_context (tier-1 doesn't touch it) with
        # no sheddable enrichment and an unrelated prompt -> the variant equals
        # the source and is dropped rather than duplicating the full call.
        big_plan = "x" * (8000 * 4)
        call = _make_call(
            prompt="UNRELATED",
            plan_context=big_plan,
            pubmed_context=None,
            ml_citations_context=None,
        )
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"fhi-internal": [_backend_with_context(8000)]},
        ):
            result = _add_proactive_shed_variants(
                [call],
                open_prompt_kwargs={},
                rebuild_prompt=self._noop_rebuild,
                original_open_prompt="DIFFERENT",
                denial_id=1,
            )
        assert result == [call]


# --- make_appeals router-call-pattern tests --------------------------------


class TestMakeAppealsRouterCallPattern:
    """Step 1 (dedupe model_names) + Step 2 (opt-out invariant)."""

    def test_opt_out_never_invokes_external_router(self):
        """Privacy guard: use_external=False must never call router with True."""
        spy, calls = _name_spy()
        _drain_make_appeals(_mock_denial(use_external=False), spy, {})
        assert all(
            c is False for c in calls
        ), f"Privacy violation: router called with use_external=True: {calls}"

    def test_opt_in_includes_external_in_backup(self):
        """When use_external=True, backup_calls path includes external."""
        spy, calls = _name_spy()
        _drain_make_appeals(_mock_denial(use_external=True), spy, {})
        assert any(
            c is True for c in calls
        ), f"backup_calls should include external when use_external=True: {calls}"

    def test_router_called_exactly_once_per_role(self):
        """Dedupe regression: primary + backup roles call router once each."""
        calls: list[bool] = []

        def spy(use_external=False):
            calls.append(use_external)
            return []  # empty -> short-circuit before retry path

        _drain_make_appeals(_mock_denial(use_external=False), spy, {})
        assert (
            len(calls) == 2
        ), f"Expected 2 router calls (primary + backup), got {len(calls)}: {calls}"


class TestBackupStageRunsOnlyWhatPrimaryDidNot:
    """The backup stage re-ran the internal calls primary had just run, so an
    opt-out denial waited for the same calls to fail the same way before the
    shed ladder started, and the zero-appeal note claimed an external
    fallback whenever consent was given, whether or not one existed."""

    @staticmethod
    def _spy(internal, external):
        def spy(use_external=False, fail_open=True):
            return list(internal) + (list(external) if use_external else [])

        return spy

    def test_opt_in_backup_is_the_externals_only(self):
        with _loguru_capture() as sink:
            _drain_make_appeals(
                _mock_denial(use_external=True),
                self._spy(["fhi-a"], ["ext-b"]),
                {},
            )
        output = sink.getvalue()
        assert "trying backup_calls" in output
        assert "models=['ext-b']" in output
        assert "external backup tried: ['ext-b']" in output

    def test_opt_out_skips_the_backup_stage(self):
        with _loguru_capture() as sink:
            _drain_make_appeals(
                _mock_denial(use_external=False), self._spy(["fhi-a"], []), {}
            )
        output = sink.getvalue()
        assert "trying backup_calls" not in output
        assert "NO EXTERNAL FALLBACK PERMITTED" in output

    def test_opt_in_without_a_selectable_external_says_so(self):
        with _loguru_capture() as sink:
            _drain_make_appeals(
                _mock_denial(use_external=True), self._spy(["fhi-a"], []), {}
            )
        output = sink.getvalue()
        assert "trying backup_calls" not in output
        assert "no external backend was selectable" in output


# --- get_model_result WARNING-log tests ------------------------------------


class TestGetModelResultLogging:
    """Step 3: backend failures must surface at WARNING (not DEBUG)."""

    def test_missing_model_logs_warning(self):
        with _loguru_capture() as sink:
            _drain_make_appeals(
                _mock_denial(),
                lambda use_external=False: ["model-that-does-not-exist"],
                {"other-model": []},
            )
        output = sink.getvalue()
        assert "not in ml_router.models_by_name" in output
        assert "model-that-does-not-exist" in output

    def test_all_backends_failed_logs_warning_with_count(self):
        backend = MagicMock(spec=RemoteFullOpenLike)
        backend.parallel_infer = MagicMock(return_value=None)
        backend.infer = MagicMock(return_value=None)
        with _loguru_capture() as sink:
            _drain_make_appeals(
                _mock_denial(),
                lambda use_external=False: ["broken-model"],
                {"broken-model": [backend, backend, backend]},
            )
        assert (
            "get_model_result: all 3 backend(s) for model_name=broken-model failed"
            in sink.getvalue()
        )


# --- diagnostics_sink + denial_text_override tests -------------------------


class TestGeneratedToAppealsTextRecording:
    """_generated_to_appeals_text feeds the attempt recorder, whose records are
    persisted against the denial. What each model actually returned -- including
    the output the pipeline refuses to deliver -- is the reason these rows
    exist, so it has to survive every branch."""

    def _recorder(self):
        return ModelAttemptRecorder(denial_id=42, generation_id="g")

    def _drain(self, recorder, future, **kwargs):
        tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
        return list(
            _generated_to_appeals_text(
                "some-model",
                future,
                tmpl,
                None,
                recorder,
                **kwargs,
            )
        )

    def test_deliverable_output_records_ok_with_the_text(self):
        recorder = self._recorder()
        appeal = "Dear insurer, " + "this claim is medically necessary. " * 20
        future = MagicMock()
        future.result.return_value = [("full", appeal)]

        self._drain(recorder, future, stage="primary", infer_type="full")

        (record,) = recorder._records
        assert record.outcome == "ok"
        assert record.stage == "primary"
        assert record.response_text == appeal
        assert record.response_chars == len(appeal)

    def test_runt_output_keeps_the_text_that_was_rejected(self):
        """A too-short response is dropped by the pipeline and used to be
        unrecoverable -- 'runt_only' told you nothing about what was said."""
        recorder = self._recorder()
        future = MagicMock()
        future.result.return_value = [("full", "no.")]

        self._drain(recorder, future)

        (record,) = recorder._records
        assert record.outcome == "runt_only"
        assert record.response_text == "no."
        assert record.error_detail  # describe_unusable_appeal said why

    def test_backend_failure_records_error_not_no_output(self):
        """A model that 500s and a model that answered with nothing are
        different problems; both used to be filed as no_output."""
        recorder = self._recorder()
        future = MagicMock()
        future.result.side_effect = TimeoutError()

        self._drain(recorder, future)

        (record,) = recorder._records
        assert record.outcome == "error"
        assert "timed out" in record.error_detail

    def test_silent_model_still_records_no_output(self):
        recorder = self._recorder()
        future = MagicMock()
        future.result.return_value = []

        self._drain(recorder, future)

        (record,) = recorder._records
        assert record.outcome == "no_output"
        assert record.response_text is None

    def test_timing_is_recorded_from_the_submit_stamp(self):
        recorder = self._recorder()
        future = MagicMock()
        future.result.return_value = []

        self._drain(recorder, future, submitted_at=time.monotonic() - 1.5)

        (record,) = recorder._records
        assert record.duration_ms is not None
        assert record.duration_ms >= 1400

    def test_deadline_abandonment_is_filed_as_abandoned_not_error(self):
        """A call the requester's deadline cut off is a budget problem, not a
        backend fault; it used to be filed as outcome=error."""
        recorder = self._recorder()
        future = MagicMock()
        future.done.return_value = False

        self._drain(recorder, future, deadline=time.monotonic() - 20)

        (record,) = recorder._records
        assert record.outcome == "abandoned"
        assert "deadline" in record.error_detail

    def test_call_skipped_for_time_is_abandoned(self):
        """_checked_infer raises DeadlineSkipped without asking the model once
        the requester's deadline (or the attempt's budget) has passed; that is
        a budget skip, not a model that failed or answered with nothing."""
        recorder = self._recorder()
        future = MagicMock()
        future.done.return_value = True
        future.result.side_effect = DeadlineSkipped("requester deadline passed")

        self._drain(recorder, future)

        (record,) = recorder._records
        assert (record.outcome, record.error_detail) == (
            "abandoned",
            "skipped: requester deadline passed",
        )

    def test_empty_answer_read_after_the_deadline_is_still_no_output(self):
        """The model was asked and answered with nothing; that its result is
        read after the deadline doesn't make it a skip."""
        recorder = self._recorder()
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = []

        self._drain(recorder, future, deadline=time.monotonic() - 1)

        (record,) = recorder._records
        assert record.outcome == "no_output"

    def test_empty_answer_before_the_deadline_is_still_no_output(self):
        recorder = self._recorder()
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = []

        self._drain(recorder, future, deadline=time.monotonic() + 300)

        (record,) = recorder._records
        assert record.outcome == "no_output"

    def test_model_unreached_on_both_tries_records_error_unavailable(self):
        """_checked_infer raises ProviderUnavailable when the first call and
        the retry both fail in transport: the row names the outage instead of
        reading as a model that answered nothing."""
        recorder = self._recorder()
        future = MagicMock()
        future.result.side_effect = ProviderUnavailable("HTTP 503 Service Unavailable")

        self._drain(recorder, future)

        (record,) = recorder._records
        assert (record.outcome, record.error_detail) == (
            "error",
            "unavailable: HTTP 503 Service Unavailable",
        )


class TestBackendLabel:
    """backend_label / RemoteModelLike.backend_descriptor: the value the
    attempt row's backend column carries. str(model) is the registry name,
    the same string as model_name, so it cannot name an endpoint."""

    def test_descriptor_names_class_wire_model_and_host_but_never_the_token(self):
        m = RemoteFullOpenLike(
            "http://h1.internal:8000/v1", "sekrit-token", "wire-model"
        )
        m.name = "fhi-2025"  # what the router stamps
        label = m.backend_descriptor()
        assert label == "RemoteFullOpenLike(wire-model @ h1.internal:8000)"
        assert "sekrit-token" not in label
        assert label != str(m)

    def test_descriptor_names_a_distinct_backup_endpoint(self):
        m = RemoteFullOpenLike(
            "http://h1:8000/v1",
            "tok",
            "wire-model",
            backup_api_base="http://h2:9000/v1",
            backup_model="backup-wire",
        )
        assert m.backend_descriptor() == (
            "RemoteFullOpenLike(wire-model @ h1:8000) +backup(backup-wire @ h2:9000)"
        )

    def test_two_instances_under_one_registry_name_get_distinct_labels(self):
        a = RemoteFullOpenLike("http://h1:8000/v1", "tok", "wire-model")
        b = RemoteFullOpenLike("http://h2:8000/v1", "tok", "wire-model")
        a.name = b.name = "fhi-2025"
        assert str(a) == str(b) == "fhi-2025"
        assert backend_label(a) != backend_label(b)

    def test_label_falls_back_to_str_for_stand_ins(self):
        # A MagicMock's backend_descriptor() is a MagicMock, not a str.
        stub = MagicMock()
        stub.__str__ = lambda self: "FakeBackend"
        assert backend_label(stub) == "FakeBackend"


class TestSummarizeModelOutcomes:
    """_summarize_model_outcomes builds the compact models_tried string."""

    def test_empty_is_empty_string(self):
        assert _summarize_model_outcomes([]) == ""

    def test_dedupes_and_sorts(self):
        # fhi-legacy appears twice with different failures: the first is kept
        # (only "ok" may displace a recorded failure), and models come out
        # sorted by name.
        out = _summarize_model_outcomes(
            [
                ("sonar", "http_429"),
                ("fhi-legacy", "no_output"),
                ("fhi-legacy", "all_backends_failed"),
            ]
        )
        assert out == "fhi-legacy:no_output,sonar:http_429"

    def test_ok_wins_over_failure_for_same_model(self):
        # A model attempted across stages: one stage empty, another produced.
        out = _summarize_model_outcomes(
            [("fhi-legacy", "no_output"), ("fhi-legacy", "ok")]
        )
        assert out == "fhi-legacy:ok"
        # Order-independent: ok still wins if seen first.
        out2 = _summarize_model_outcomes(
            [("fhi-legacy", "ok"), ("fhi-legacy", "no_output")]
        )
        assert out2 == "fhi-legacy:ok"

    def test_real_attempt_wins_over_never_called_reason(self):
        # A model gets one record per infer_type. When the denial has no
        # procedure the medically_necessary call is skipped as "no_prompt" and
        # -- because that is recorded synchronously, before any future is
        # drained -- it used to mask the reason the `full` call actually
        # failed. The wire outcome is what triage needs.
        out = _summarize_model_outcomes(
            [("fhi-legacy", "no_prompt"), ("fhi-legacy", "all_backends_failed")]
        )
        assert out == "fhi-legacy:all_backends_failed"

    def test_never_called_reason_kept_when_it_is_all_we_have(self):
        out = _summarize_model_outcomes(
            [("fhi-legacy", "no_prompt"), ("sonar", "not_registered")]
        )
        assert out == "fhi-legacy:no_prompt,sonar:not_registered"

    def test_ok_wins_over_never_called_reason(self):
        out = _summarize_model_outcomes(
            [("fhi-legacy", "ok"), ("fhi-legacy", "no_prompt")]
        )
        assert out == "fhi-legacy:ok"

    def test_none_model_name_becomes_unknown(self):
        assert _summarize_model_outcomes([(None, "no_output")]) == "unknown:no_output"

    def test_caps_long_lists(self):
        outcomes = [(f"m{i}", "no_output") for i in range(20)]
        summary = _summarize_model_outcomes(outcomes)
        assert "+8_more" in summary
        # 12 shown + the "+N_more" marker.
        assert len(summary.split(",")) == 13


class TestMakeAppealsDiagnosticsSink:
    """make_appeals reports which stage produced the first appeal (or 'none')
    via diagnostics_sink so the generating-phase logging/done-frame can show
    whether the primary won or a shed-tier retry rescued it, plus which models
    were tried."""

    def test_sink_records_none_when_all_stages_empty(self):
        sink: dict = {}
        gen = AppealGenerator()
        tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=lambda use_external=False: [],
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ), patch(
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):
            list(
                gen.make_appeals(
                    _mock_denial(),
                    tmpl,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    diagnostics_sink=sink,
                )
            )
        assert sink.get("winning_stage") == "none"
        assert sink.get("shed_tier") is None
        assert "models_tried" in sink

    def test_sink_records_not_registered_models(self):
        """A requested model absent from the router is recorded as
        not_registered in models_tried."""
        sink: dict = {}
        gen = AppealGenerator()
        tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=lambda use_external=False: ["ghost-model"],
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ), patch(
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):
            list(
                gen.make_appeals(
                    _mock_denial(),
                    tmpl,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    diagnostics_sink=sink,
                )
            )
        assert "ghost-model:not_registered" in sink.get("models_tried", "")


class TestDenialTextOverride:
    """denial_text_override substitutes a summary for the raw denial text in
    the prompt (used only for oversized denials); None keeps full context."""

    def _spy_prompt_denial_text(self, denial_text_override):
        gen = AppealGenerator()
        tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
        seen: dict = {}

        def spy_prompt(**kwargs):
            seen.update(kwargs)
            return "PROMPT"

        with patch.object(gen, "make_open_prompt", side_effect=spy_prompt), patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=lambda use_external=False: [],
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ), patch(
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):
            try:
                list(
                    gen.make_appeals(
                        _mock_denial(),  # denial_text="denial"
                        tmpl,
                        medical_reasons=[],
                        non_ai_appeals=[],
                        denial_text_override=denial_text_override,
                    )
                )
            except Exception:
                pass
        return seen

    def test_override_used_in_prompt_when_provided(self):
        seen = self._spy_prompt_denial_text("CONDENSED SUMMARY")
        assert seen.get("denial_text") == "CONDENSED SUMMARY"

    def test_full_denial_text_used_when_override_is_none(self):
        seen = self._spy_prompt_denial_text(None)
        assert seen.get("denial_text") == "denial"


# --- _peek_real_or_none: runt-first fallback regression -------------------


class TestPeekRealOrNone:
    """Regression: a runt first item must trigger fallback. Without this,
    downstream filtering (is_real_appeal) drops the runt and the user gets
    zero appeals — even though backup/retry paths might have produced
    valid drafts."""

    def test_runt_first_returns_none(self):
        first, _ = _peek_real_or_none(iter([_ga("x")]), denial_id=1, stage="primary")
        assert first is None

    def test_empty_string_first_returns_none(self):
        first, _ = _peek_real_or_none(iter([_ga("")]), denial_id=1, stage="primary")
        assert first is None

    def test_whitespace_first_returns_none(self):
        first, _ = _peek_real_or_none(iter([_ga("   ")]), denial_id=1, stage="primary")
        assert first is None

    def test_real_first_passes_through(self):
        real = _ga("this is a long enough appeal text for delivery")
        first, rest = _peek_real_or_none(iter([real]), denial_id=1, stage="primary")
        assert first is real
        # Real item chained back so caller can stream from `rest`
        assert next(rest) is real

    def test_empty_iter_returns_none(self):
        first, _ = _peek_real_or_none(iter([]), denial_id=1, stage="primary")
        assert first is None

    def test_runt_logs_warning_with_stage_and_denial_id(self):
        with _loguru_capture() as sink:
            _peek_real_or_none(iter([_ga("short")]), denial_id=999, stage="primary")
        output = sink.getvalue()
        assert "primary produced an unusable item" in output
        assert "denial 999" in output

    def test_scans_past_runt_to_real_item_in_same_stage(self):
        """A fast model's runt must not abandon the whole stage: the peek
        skips it and returns the real appeal a slower model in the SAME
        stage produced."""
        real = _ga("this is a long enough appeal text for delivery")
        first, rest = _peek_real_or_none(
            iter([_ga("no."), _ga(""), real]), denial_id=1, stage="primary"
        )
        assert first is real
        assert next(rest) is real

    def test_scan_records_each_rejected_item(self):
        recorded = []
        recorder = MagicMock()
        recorder.record.side_effect = lambda rec: recorded.append(rec)
        real = _ga("this is a long enough appeal text for delivery")
        first, _ = _peek_real_or_none(
            iter([_ga("no."), real]), denial_id=1, stage="primary", recorder=recorder
        )
        assert first is real
        assert [r.outcome for r in recorded] == ["rejected_at_peek"]
        assert recorded[0].response_text == "no."

    def test_scan_records_the_call_variant_and_backend_of_the_rejected_item(self):
        """The peek row is the one that explains a stage fall-through; it used
        to carry only the model, so it could not say whether the runt came
        from the full or the medically_necessary call, nor from which
        backend."""
        from fighthealthinsurance.generate_appeal import GeneratedAppeal

        recorded = []
        recorder = MagicMock()
        recorder.record.side_effect = lambda rec: recorded.append(rec)
        runt = GeneratedAppeal(
            text="no.",
            model_name="m",
            infer_type="medically_necessary",
            backend="Fake(wire @ h:1)",
        )
        _peek_real_or_none(iter([runt]), denial_id=1, stage="backup", recorder=recorder)
        (rec,) = recorded
        assert rec.infer_type == "medically_necessary"
        assert rec.backend == "Fake(wire @ h:1)"

    def test_wordless_first_returns_none(self):
        """A long but wordless first item (e.g. a model echoing back the claim
        number and date of service) must trigger fallback just like a runt
        does."""
        first, _ = _peek_real_or_none(
            iter([_ga("1234-5678-90, 11/02/2026: $1,250.00")]),
            denial_id=1,
            stage="primary",
        )
        assert first is None


# --- failing models: first pass, shed ladder, extraction -------------------

_REAL_APPEAL = "Dear insurer, " + "this claim is medically necessary. " * 20


def _fake_backend(*, external, available=True, infer_result=None, infer_error=None):
    """A backend reached through make_appeals' sync ``infer`` seam, with the
    in-memory signals the router and the shed ladder read."""
    backend = MagicMock()
    backend.external = external
    backend.is_available.return_value = available
    backend._spend_allows.return_value = True
    backend.get_max_context.return_value = 128000
    if infer_error is not None:
        backend.infer.side_effect = infer_error
    else:
        backend.infer.return_value = (
            infer_result if infer_result is not None else [("full", _REAL_APPEAL)]
        )
    return backend


def _names_by_role(internal_up, internal_all, hosted):
    """A generate_text_backend_names stand-in following the router's rules:
    internals fail open only when asked to, and with use_external only when
    no hosted model is selectable. Returns (fn, calls) where calls records
    each (use_external, fail_open) asked for."""
    calls: list[tuple] = []

    def names(use_external=False, fail_open=True):
        calls.append((use_external, fail_open))
        if use_external:
            internal = internal_up or ([] if hosted else internal_all)
            return list(internal)[:6] + list(hosted)
        return list(internal_up or (internal_all if fail_open else []))

    return names, calls


def _run_make_appeals(denial, names_fn, models_by_name, **kwargs):
    """Drive make_appeals to the end. Returns its diagnostics sink."""
    sink: dict = {}
    gen = AppealGenerator()
    tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=names_fn,
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new=models_by_name,
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        list(
            gen.make_appeals(
                denial,
                tmpl,
                medical_reasons=[],
                non_ai_appeals=[],
                diagnostics_sink=sink,
                **kwargs,
            )
        )
    return sink


class TestOptInFirstPassSkipsDownInternals:
    """With every internal model known down and a hosted model up, an opted-in
    appeal's first pass used to fail open to the dead internals: they hung for
    their whole timeout and the hosted backup was abandoned at the deadline
    before it could answer."""

    def _all_internals_down(self):
        internal = _fake_backend(external=False)
        hosted = _fake_backend(external=True)
        names, calls = _names_by_role([], ["fhi-a"], ["ext-b"])
        return internal, hosted, names, calls, {"fhi-a": [internal], "ext-b": [hosted]}

    def test_first_pass_asks_for_internals_without_failing_open(self):
        _internal, _hosted, names, calls, models = self._all_internals_down()

        _run_make_appeals(_mock_denial(use_external=True), names, models)

        assert [fail_open for use_ext, fail_open in calls if not use_ext] == [False]

    def test_down_internal_is_never_asked(self):
        internal, _hosted, names, _calls, models = self._all_internals_down()

        _run_make_appeals(_mock_denial(use_external=True), names, models)

        assert not internal.infer.called

    def test_hosted_backup_writes_the_letter(self):
        _internal, _hosted, names, _calls, models = self._all_internals_down()

        sink = _run_make_appeals(_mock_denial(use_external=True), names, models)

        assert sink["winning_stage"] == "backup"

    def test_empty_first_pass_is_not_logged_as_an_error(self):
        _internal, _hosted, names, _calls, models = self._all_internals_down()

        with _loguru_capture(level="ERROR") as log:
            _run_make_appeals(_mock_denial(use_external=True), names, models)

        assert "zero internal model names" not in log.getvalue()

    def test_specialized_hint_call_is_not_sent_to_a_down_internal(self):
        """The hint call goes to the strongest internal model, which the
        router still names (its last resort) when every internal is down."""
        internal, _hosted, names, _calls, models = self._all_internals_down()

        with patch.object(
            AppealGenerator, "_best_internal_model_name", return_value="fhi-a"
        ), patch.object(
            AppealGenerator, "_build_specialized_hint_block", return_value="HINTS"
        ):
            _run_make_appeals(
                _mock_denial(use_external=True),
                names,
                models,
                specialized_templates=[MentalHealthParityAppeal],
            )

        assert not internal.infer.called

    def test_without_a_hosted_model_down_internals_stay_the_last_resort(self):
        internal = _fake_backend(external=False)
        names, _calls = _names_by_role([], ["fhi-a"], [])

        _run_make_appeals(_mock_denial(use_external=True), names, {"fhi-a": [internal]})

        assert internal.infer.called

    def test_opt_out_keeps_the_fail_open_first_pass(self):
        internal = _fake_backend(external=False)
        names, calls = _names_by_role([], ["fhi-a"], [])

        _run_make_appeals(
            _mock_denial(use_external=False), names, {"fhi-a": [internal]}
        )

        assert all(fail_open for _use_ext, fail_open in calls)


class TestCallsWorthShedding:
    """Shedding context can rescue a model that overflowed it, never one that
    could not be asked: the ladder used to re-ask unavailable models at both
    tiers on every appeal for the length of an outage."""

    def _keep(self, models_by_name, names=("fhi-a",)):
        calls = [_make_call(model_name=name) for name in names]
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new=models_by_name,
        ):
            return [c["model_name"] for c in _calls_worth_shedding(calls)]

    def test_drops_a_model_with_no_instance_that_can_be_asked(self):
        parked = _fake_backend(external=False, available=False)

        assert self._keep({"fhi-a": [parked]}) == []

    def test_drops_a_model_whose_provider_is_out_of_budget(self):
        paused = _fake_backend(external=False)
        paused._spend_allows.return_value = False

        assert self._keep({"fhi-a": [paused]}) == []

    def test_keeps_a_model_that_can_still_be_asked(self):
        backend = _fake_backend(external=False)

        assert self._keep({"fhi-a": [backend]}) == ["fhi-a"]

    def test_keeps_a_model_whose_signal_cannot_be_read(self):
        backend = _fake_backend(external=False)
        backend.is_available.side_effect = RuntimeError("boom")

        assert self._keep({"fhi-a": [backend]}) == ["fhi-a"]

    def test_keeps_only_the_models_that_could_still_answer(self):
        parked = _fake_backend(external=False, available=False)
        up = _fake_backend(external=False)

        kept = self._keep({"fhi-a": [parked], "fhi-b": [up]}, names=("fhi-a", "fhi-b"))

        assert kept == ["fhi-b"]


class TestShedLadderSkipsUnavailableModels:
    """make_appeals runs the context-shed ladder only over primary models that
    could still answer, and skips the ladder (and its sleep) when none can."""

    def _run(self, models_by_name):
        names, _calls = _names_by_role(list(models_by_name), [], [])
        return _run_make_appeals(
            _mock_denial(use_external=False), names, models_by_name
        )

    @staticmethod
    def _stages(sink):
        return {r.stage for r in sink["attempt_recorder"]._records}

    def test_parked_pool_skips_the_retry_and_its_sleep(self):
        """Read from the log rather than the sleep mock: patching time.sleep
        patches it process-wide, and background threads sleep too."""
        parked = _fake_backend(
            external=False,
            available=False,
            infer_error=ProviderUnavailable("not served here"),
        )

        with _loguru_capture() as log:
            self._run({"fhi-a": [parked]})

        assert "retrying primary" not in log.getvalue()

    def test_parked_pool_records_no_retry_rows(self):
        parked = _fake_backend(
            external=False,
            available=False,
            infer_error=ProviderUnavailable("not served here"),
        )

        sink = self._run({"fhi-a": [parked]})

        assert self._stages(sink) == {"primary"}

    def test_model_parked_by_its_refusal_is_not_retried(self):
        refused = _fake_backend(external=False)

        def refuse(*_args, **_kwargs):
            refused.is_available.return_value = False
            raise ProviderUnavailable("refused recently (key or account)")

        refused.infer.side_effect = refuse

        sink = self._run({"fhi-a": [refused]})

        assert self._stages(sink) == {"primary"}

    def test_askable_model_whose_every_row_says_unavailable_is_retried(self):
        """A primary that overflowed its context beside a backup leg cooling
        down reports "unavailable:" on every try, yet was reached: shedding
        context is what rescues it, so its rows must not drop it."""
        overflowed = _fake_backend(
            external=False,
            infer_error=ProviderUnavailable("fhi-a via backup: cooling down"),
        )

        sink = self._run({"fhi-a": [overflowed]})

        assert {"retry_tier_1", "retry_tier_2"} <= self._stages(sink)

    def test_ladder_still_retries_the_model_that_answered_a_runt(self):
        parked = _fake_backend(
            external=False,
            available=False,
            infer_error=ProviderUnavailable("not served here"),
        )
        runty = _fake_backend(external=False, infer_result=[("full", "no.")])

        sink = self._run({"fhi-a": [parked], "fhi-b": [runty]})

        retried = {
            r.model_name
            for r in sink["attempt_recorder"]._records
            if r.stage.startswith("retry_tier_")
        }
        assert retried == {"fhi-b"}


class TestProcedureDiagnosisUnavailableModel:
    """A model that cannot be asked used to reach the fan-out as an unexpected
    error, logging a WARNING with a full traceback per model per denial."""

    def _generator(self, model):
        gen = AppealGenerator()
        regex = MagicMock()
        regex.get_procedure_and_diagnosis = AsyncMock(return_value=(None, None))
        gen.regex_denial_processor = regex
        model.get_procedure_and_diagnosis = AsyncMock(
            side_effect=ProviderUnavailable("refused recently (key or account)")
        )
        return gen

    async def _extract(self, gen, backends):
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.entity_extract_backends",
            return_value=backends,
        ):
            return await gen.get_procedure_and_diagnosis("denial text")

    @pytest.mark.asyncio
    async def test_unavailable_model_logs_no_fanout_traceback(self):
        model = MagicMock()
        gen = self._generator(model)

        with _loguru_capture() as log:
            try:
                await self._extract(gen, [model])
            except ExtractionUnavailable:
                pass

        assert "Task error" not in log.getvalue()

    @pytest.mark.asyncio
    async def test_unavailable_model_alone_still_raises_extraction_unavailable(self):
        model = MagicMock()
        gen = self._generator(model)

        with pytest.raises(ExtractionUnavailable):
            await self._extract(gen, [model])

    @pytest.mark.asyncio
    async def test_another_models_answer_is_still_used(self):
        down = MagicMock()
        gen = self._generator(down)
        up = MagicMock()
        up.get_procedure_and_diagnosis = AsyncMock(return_value=("MRI", "back pain"))

        result = await self._extract(gen, [down, up])

        assert result == ("MRI", "back pain")

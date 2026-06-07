import io
from contextlib import contextmanager
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from loguru import logger as loguru_logger
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
    GeneratedAppeal,
    _peek_real_or_none,
    _shed_context,
    _PROMPT_TIER1_NULLS,
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
        assert result[0][1] == "Yes. Was it localized to the left MCA? Yes, it was."

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

        # Verify the result has correct question-answer pairs
        assert len(result) == 2
        assert result[0][0] == "This treatment is necessary?"
        assert result[0][1] == ""
        assert result[1][0] == "Patient history includes condition X?"
        assert result[1][1] == ""

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

        # Verify the result is an empty list
        assert result == []

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

        # Verify the result is an empty list since we should reject responses with "Rationale for questions"
        assert result == []

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

    def spy(use_external=False):
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
        "medication_context": "med ctx",
    }
    base.update(overrides)
    return base


class TestShedContextPromptRebuild:
    """Tier shedding must re-render the prompt with enrichment stripped.

    Without this, pubmed/citations/rag/nice/uspstf/etc. stay baked into
    ``open_prompt`` even after the call-dict copies are nulled, so the
    retry token count doesn't actually drop. Regression cover for
    PR #811 follow-up."""

    def test_tier1_rebuilds_prompt_with_enrichment_nulled(self):
        kwargs = _prompt_kwargs()
        seen_kwargs: dict = {}

        def rebuild(**rk):
            seen_kwargs.update(rk)
            return "REBUILT-PROMPT"

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
        seen_kwargs: dict = {}

        def rebuild(**rk):
            seen_kwargs.update(rk)
            return "REBUILT"

        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=1,
            open_prompt_kwargs=_prompt_kwargs(clinical_trials_context="NCT0123 ..."),
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert seen_kwargs["clinical_trials_context"] is None
        assert "prompt.clinical_trials_context" in changed

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
        seen_kwargs: dict = {}

        def rebuild(**rk):
            seen_kwargs.update(rk)
            return "OUT"

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
        seen_kwargs: dict = {}

        def rebuild(**rk):
            seen_kwargs.update(rk)
            return "OUT"

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
        calls_count = [0]

        def rebuild(**_):
            calls_count[0] += 1
            return "SHOULD-NOT-BE-USED"

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
        # Regression (Copilot review on PR #824): a whitespace-only
        # enrichment value is NOT a no-op in make_open_prompt --- the gate
        # there is ``is not None and != ""``, so ``"   "`` still trips
        # has_citations and renders the CITATION INSTRUCTIONS block plus
        # the per-section header (``Provided citations (use these):    ``).
        # Those bytes need to drop on retry, so the shed pass must null
        # whitespace-only values and trigger a rebuild.
        whitespace_kwargs = {key: "   \n\t" for key in _PROMPT_TIER1_NULLS}
        kwargs = _prompt_kwargs(**whitespace_kwargs)
        seen_kwargs: dict = {}

        def rebuild(**rk):
            seen_kwargs.update(rk)
            return "SHED"

        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=1,
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        # Every whitespace-only enrichment is nulled and reported.
        for key in _PROMPT_TIER1_NULLS:
            assert seen_kwargs[key] is None, f"{key} should have been nulled"
            assert f"prompt.{key}" in changed

    def test_truly_empty_string_enrichment_is_skipped(self):
        # The truly-empty string ``""`` IS already a no-op in
        # make_open_prompt (gate fails on ``!= ""``), so it can be skipped
        # without adding diagnostic noise to ``changed``. Pin that the
        # truly-empty case still avoids a rebuild call.
        empty_kwargs = {key: "" for key in _PROMPT_TIER1_NULLS}
        kwargs = _prompt_kwargs(**empty_kwargs, plan_context="short")
        calls_count = [0]

        def rebuild(**_):
            calls_count[0] += 1
            return "X"

        _, changed = _shed_context(
            [_make_call(prompt="ORIGINAL")],
            tier=2,
            open_prompt_kwargs=kwargs,
            rebuild_prompt=rebuild,
            original_open_prompt="ORIGINAL",
        )
        assert calls_count[0] == 0, "rebuild fired on truly-empty enrichment"
        assert not any(c.startswith("prompt.") for c in changed)


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
        first, _ = _peek_real_or_none(
            iter([_ga("   ")]), denial_id=1, stage="primary"
        )
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
            _peek_real_or_none(
                iter([_ga("short")]), denial_id=999, stage="primary"
            )
        output = sink.getvalue()
        assert "primary first item is a runt" in output
        assert "denial 999" in output

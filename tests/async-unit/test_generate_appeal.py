import io
from contextlib import contextmanager
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from loguru import logger as loguru_logger
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
    _peek_real_or_none,
    _shed_context,
    _SHEDDABLE_TIER1,
    _SHEDDABLE_TIER2,
    _TIER3_TRUNCATIONS,
)


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
    """Build a `calls`-shape dict matching make_appeals' schema."""
    base = {
        "model_name": "fhi-internal",
        "prompt": "Please write an appeal.",
        "patient_context": "patient medical history",
        "plan_context": "plan documents summary",
        "infer_type": "full",
        "pubmed_context": "pubmed citations",
        "ml_citations_context": ["citation-1", "citation-2"],
        "prof_pov": False,
        "nice_context": "nice guidelines",
        "rag_context": "rag context",
        "pa_context": "pa context",
        "uspstf_context": "uspstf context",
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

    @pytest.mark.parametrize(
        "tier,expected_nulled",
        [
            (1, _SHEDDABLE_TIER1),
            (2, _SHEDDABLE_TIER1 + _SHEDDABLE_TIER2),
        ],
    )
    def test_drops_by_tier(self, tier, expected_nulled):
        new_calls, changed = _shed_context([_make_call()], tier=tier)
        new = new_calls[0]
        for key in expected_nulled:
            assert new[key] is None
        # Core context never dropped by tier 1/2
        assert new["plan_context"] == "plan documents summary"
        assert new["patient_context"] == "patient medical history"
        assert set(changed) == set(expected_nulled)

    def test_tier3_truncates_core_when_over_cap(self):
        oversized = {key: "X" * (cap + 100) for key, cap in _TIER3_TRUNCATIONS}
        new_calls, changed = _shed_context([_make_call(**oversized)], tier=3)
        new = new_calls[0]
        for key, cap in _TIER3_TRUNCATIONS:
            assert len(new[key]) == cap
            assert f"{key}(truncated)" in changed

    def test_tier3_skips_truncation_under_cap(self):
        new_calls, changed = _shed_context(
            [_make_call(plan_context="short", patient_context="also short")],
            tier=3,
        )
        new = new_calls[0]
        assert new["plan_context"] == "short"
        assert new["patient_context"] == "also short"
        assert not any("(truncated)" in c for c in changed)

    def test_does_not_mutate_input_calls(self):
        original = _make_call()
        _shed_context([original], tier=3)
        assert original["uspstf_context"] == "uspstf context"
        assert original["pubmed_context"] == "pubmed citations"

    def test_already_none_inputs_not_reported_as_changed(self):
        _, changed = _shed_context(
            [_make_call(pubmed_context=None, plan_context=None)], tier=3
        )
        assert "pubmed_context" not in changed


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
        first, _ = _peek_real_or_none(iter(["x"]), denial_id=1, stage="primary")
        assert first is None

    def test_empty_string_first_returns_none(self):
        first, _ = _peek_real_or_none(iter([""]), denial_id=1, stage="primary")
        assert first is None

    def test_whitespace_first_returns_none(self):
        first, _ = _peek_real_or_none(iter(["   "]), denial_id=1, stage="primary")
        assert first is None

    def test_real_first_passes_through(self):
        real = "this is a long enough appeal text for delivery"
        first, rest = _peek_real_or_none(iter([real]), denial_id=1, stage="primary")
        assert first == real
        # Real item chained back so caller can stream from `rest`
        assert next(rest) == real

    def test_empty_iter_returns_none(self):
        first, _ = _peek_real_or_none(iter([]), denial_id=1, stage="primary")
        assert first is None

    def test_runt_logs_warning_with_stage_and_denial_id(self):
        with _loguru_capture() as sink:
            _peek_real_or_none(iter(["short"]), denial_id=999, stage="primary")
        output = sink.getvalue()
        assert "primary first item is a runt" in output
        assert "denial 999" in output

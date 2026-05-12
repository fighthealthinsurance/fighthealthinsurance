from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
    _shed_context,
    _SHEDDABLE_TIER1,
    _SHEDDABLE_TIER2,
    _TIER3_PLAN_CAP,
    _TIER3_PATIENT_CAP,
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


def _make_call(**overrides):
    """Build a `calls`-shape dict matching make_appeals' call schema."""
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


class TestShedContext:
    """Direct unit tests for the _shed_context helper used by make_appeals
    retry path when zero-result failures may stem from oversized prompts."""

    def test_tier1_drops_enrichments_only(self):
        calls = [_make_call()]
        new_calls, changed = _shed_context(calls, tier=1)
        assert len(new_calls) == 1
        new = new_calls[0]
        # Tier 1 keys are nulled
        for key in _SHEDDABLE_TIER1:
            assert new[key] is None
        # Tier 2 keys are untouched
        for key in _SHEDDABLE_TIER2:
            assert new[key] == calls[0][key]
        # Core context untouched
        assert new["plan_context"] == "plan documents summary"
        assert new["patient_context"] == "patient medical history"
        # Reports what changed
        assert set(changed) == set(_SHEDDABLE_TIER1)

    def test_tier2_drops_pubmed_and_citations(self):
        calls = [_make_call()]
        new_calls, changed = _shed_context(calls, tier=2)
        new = new_calls[0]
        # Tier 1 + Tier 2 keys all nulled
        for key in (*_SHEDDABLE_TIER1, *_SHEDDABLE_TIER2):
            assert new[key] is None
        # Core context untouched
        assert new["plan_context"] == "plan documents summary"
        assert new["patient_context"] == "patient medical history"
        assert set(changed) == set(_SHEDDABLE_TIER1) | set(_SHEDDABLE_TIER2)

    def test_tier3_truncates_core(self):
        big_plan = "A" * (_TIER3_PLAN_CAP + 100)
        big_patient = "B" * (_TIER3_PATIENT_CAP + 100)
        calls = [_make_call(plan_context=big_plan, patient_context=big_patient)]
        new_calls, changed = _shed_context(calls, tier=3)
        new = new_calls[0]
        assert len(new["plan_context"]) == _TIER3_PLAN_CAP
        assert len(new["patient_context"]) == _TIER3_PATIENT_CAP
        assert "plan_context(truncated)" in changed
        assert "patient_context(truncated)" in changed

    def test_tier3_no_truncation_when_under_cap(self):
        calls = [_make_call(plan_context="short", patient_context="also short")]
        new_calls, changed = _shed_context(calls, tier=3)
        new = new_calls[0]
        assert new["plan_context"] == "short"
        assert new["patient_context"] == "also short"
        # Not in changed because nothing was truncated
        assert "plan_context(truncated)" not in changed
        assert "patient_context(truncated)" not in changed

    def test_does_not_mutate_input(self):
        original = _make_call()
        calls = [original]
        _shed_context(calls, tier=3)
        # Original dict object unchanged
        assert original["uspstf_context"] == "uspstf context"
        assert original["pubmed_context"] == "pubmed citations"

    def test_handles_none_inputs_gracefully(self):
        calls = [_make_call(pubmed_context=None, plan_context=None)]
        new_calls, changed = _shed_context(calls, tier=3)
        new = new_calls[0]
        assert new["pubmed_context"] is None
        # None values don't get reported as "changed" since they were already None
        assert "pubmed_context" not in changed


class TestMakeAppealsRouterCallPattern:
    """Regression tests for Step 1 (dedupe model_names) and Step 2 (opt-out
    invariant: never call router with use_external=True when denial.use_external=False).

    These tests force the full failure path (primary + backup + retry all
    empty) by mocking ml_router.models_by_name to be empty and verify the
    router call pattern.
    """

    def _build_denial_mock(self, use_external: bool):
        denial = MagicMock()
        denial.denial_id = 999
        denial.use_external = use_external
        denial.qa_context = None
        denial.health_history = None
        denial.plan_context = None
        denial.plan_documents_summary = None
        denial.professional_to_finish = False
        denial.candidate_procedure = None
        denial.candidate_diagnosis = None
        denial.your_state = None
        denial.diagnosis = "test diagnosis"
        denial.procedure = "test procedure"
        denial.denial_text = "test denial text"
        denial.insurance_company = "Test Insurance"
        denial.claim_id = None
        denial.appeal_fax_number = None
        denial.your_state = None
        denial.employer_name = None
        denial.plan_id = None
        return denial

    def test_opt_out_never_calls_router_with_use_external_true(self):
        """Privacy regression guard: when denial.use_external=False, the
        router must NEVER be called with use_external=True — even when
        primary, backup, and retry all fail."""
        denial = self._build_denial_mock(use_external=False)
        template_gen = AppealTemplateGenerator(
            prefaces=["Preface"], main=["Main body"], footer=["Footer"]
        )

        gen = AppealGenerator()
        external_calls: list[bool] = []

        def spy_generate_text_backend_names(use_external=False):
            external_calls.append(use_external)
            # Return an internal-only model name so calls list is non-empty
            # but models_by_name lookup will return [] -> get_model_result
            # returns [] -> as_available_nested yields nothing
            return ["nonexistent-internal-model"]

        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=spy_generate_text_backend_names,
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},  # Empty -> get_model_result returns [] for every name
        ), patch(
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):  # Skip the 1s backoff in tests
            try:
                appeals = gen.make_appeals(
                    denial,
                    template_gen,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                )
                # Drain the iterator — initial_appeals may include the static
                # template; we don't care about the count, only the spy.
                list(appeals)
            except Exception:
                # If something unrelated fails, the spy assertions below
                # still catch the privacy violation.
                pass

        # The KEY assertion: no call passed use_external=True
        assert all(external is False for external in external_calls), (
            f"Privacy violation: router was called with use_external=True "
            f"when denial.use_external=False. Calls: {external_calls}"
        )

    def test_opt_in_includes_external_in_backup(self):
        """Step 1 regression: when use_external=True, backup_calls must
        include external models (router called once with True)."""
        denial = self._build_denial_mock(use_external=True)
        template_gen = AppealTemplateGenerator(
            prefaces=["Preface"], main=["Main body"], footer=["Footer"]
        )

        gen = AppealGenerator()
        external_calls: list[bool] = []

        def spy(use_external=False):
            external_calls.append(use_external)
            return ["nonexistent-model"]

        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=spy,
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ), patch(
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):
            try:
                appeals = gen.make_appeals(
                    denial,
                    template_gen,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                )
                list(appeals)
            except Exception:
                pass

        # At least one call should be use_external=True (the backup_calls path)
        assert any(external is True for external in external_calls), (
            f"Step 1 regression: backup_calls should include external models "
            f"when denial.use_external=True. Calls: {external_calls}"
        )

    def test_primary_calls_router_once_per_role(self):
        """Step 1 regression: the duplicate `generate_text_backend_names() +
        generate_text_backend_names()` pattern has been removed. Primary and
        backup each call the router once (not twice each)."""
        denial = self._build_denial_mock(use_external=False)
        template_gen = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])

        gen = AppealGenerator()
        external_calls: list[bool] = []

        def spy(use_external=False):
            external_calls.append(use_external)
            # Return empty -> empty calls/backup_calls -> short-circuits before retry
            return []

        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=spy,
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={},
        ), patch(
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):
            try:
                appeals = gen.make_appeals(
                    denial,
                    template_gen,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                )
                list(appeals)
            except Exception:
                pass

        # Each role (primary, backup) calls router exactly once during the
        # call-list-construction phase. There may be additional calls during
        # the retry path, but the build phase before any execution should
        # show exactly 2 calls (primary use_external=False + backup with
        # denial.use_external=False).
        # We assert <= 2 calls happened during build phase: the test setup
        # makes models_by_name empty so the spy is called when
        # generate_text_backend_names is invoked during make_appeals setup.
        # The exact count depends on whether the retry path engages (it does
        # not call router again — it reuses `calls`).
        assert len(external_calls) == 2, (
            f"Step 1 regression: expected exactly 2 router calls (one for "
            f"primary, one for backup). Got {len(external_calls)}: {external_calls}"
        )


class TestGetModelResultLogging:
    """Step 3 regression: backend failures must surface at WARNING (was
    DEBUG, which is filtered out in production)."""

    def test_missing_model_logs_warning(self, caplog):
        """When a requested model_name isn't in models_by_name, log at WARNING
        with available-names sample so the misconfiguration is visible."""
        # get_model_result is a closure inside make_appeals; the most
        # reliable way to exercise it is through make_appeals itself.
        denial = MagicMock()
        denial.denial_id = 42
        denial.use_external = False
        denial.qa_context = None
        denial.health_history = None
        denial.plan_context = None
        denial.plan_documents_summary = None
        denial.professional_to_finish = False
        denial.diagnosis = "dx"
        denial.procedure = "px"
        denial.denial_text = "denial"
        denial.insurance_company = "ins"
        denial.claim_id = None

        template_gen = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
        gen = AppealGenerator()

        # The router returns a name; models_by_name lookup yields nothing.
        from loguru import logger as loguru_logger
        import io

        sink = io.StringIO()
        handler_id = loguru_logger.add(sink, level="WARNING")
        try:
            with patch(
                "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
                return_value=["model-that-does-not-exist"],
            ), patch(
                "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
                new={"other-model": []},
            ), patch(
                "fighthealthinsurance.generate_appeal.time.sleep"
            ):
                try:
                    list(
                        gen.make_appeals(
                            denial,
                            template_gen,
                            medical_reasons=[],
                            non_ai_appeals=[],
                            pubmed_context=None,
                            ml_citations_context=None,
                            plan_context=None,
                        )
                    )
                except Exception:
                    pass

            output = sink.getvalue()
            assert "not in ml_router.models_by_name" in output, (
                f"Step 3 regression: expected WARNING about missing model. "
                f"Got:\n{output}"
            )
            assert "model-that-does-not-exist" in output, (
                f"WARNING should include the requested model name. " f"Got:\n{output}"
            )
        finally:
            loguru_logger.remove(handler_id)

    def test_all_backends_failed_logs_warning_with_count(self):
        """When all backends fail, log at WARNING with the backend count
        so we can correlate with deploys (e.g. degraded cluster sizes)."""
        denial = MagicMock()
        denial.denial_id = 43
        denial.use_external = False
        denial.qa_context = None
        denial.health_history = None
        denial.plan_context = None
        denial.plan_documents_summary = None
        denial.professional_to_finish = False
        denial.diagnosis = "dx"
        denial.procedure = "px"
        denial.denial_text = "denial"
        denial.insurance_company = "ins"
        denial.claim_id = None

        template_gen = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
        gen = AppealGenerator()

        # Configure 3 backends that return None from parallel_infer.
        # _get_model_result returns None (no fallback Future is produced
        # since parallel_infer didn't raise — it just returned None), so
        # get_model_result's loop iterates past all backends and emits the
        # "All N backend(s) failed" WARNING at the end.
        from loguru import logger as loguru_logger
        import io

        failing_backend = MagicMock(spec=RemoteFullOpenLike)
        failing_backend.parallel_infer = MagicMock(return_value=None)
        failing_backend.infer = MagicMock(return_value=None)

        sink = io.StringIO()
        handler_id = loguru_logger.add(sink, level="WARNING")
        try:
            with patch(
                "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
                return_value=["broken-model"],
            ), patch(
                "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
                new={
                    "broken-model": [failing_backend, failing_backend, failing_backend]
                },
            ), patch(
                "fighthealthinsurance.generate_appeal.time.sleep"
            ):
                try:
                    list(
                        gen.make_appeals(
                            denial,
                            template_gen,
                            medical_reasons=[],
                            non_ai_appeals=[],
                            pubmed_context=None,
                            ml_citations_context=None,
                            plan_context=None,
                        )
                    )
                except Exception:
                    pass

            output = sink.getvalue()
            assert "All 3 backend(s) for model_name=broken-model failed" in output, (
                f"Step 3 regression: expected WARNING with backend count. "
                f"Got:\n{output}"
            )
        finally:
            loguru_logger.remove(handler_id)

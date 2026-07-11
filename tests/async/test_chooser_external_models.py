"""Chooser candidate generation must include external backends and persist
their registry names.

Regression tests for the two bugs that kept external models (Anthropic
Claude, Azure, Groq, ...) out of the model-selection UI and the ML usage
dashboard:

1. Candidate generation called the router with its internal-only default
   (``use_external=False``), so external backends never produced candidates.
2. Chat candidate generation passed ``current_message=`` to
   ``generate_chat_response`` (whose parameter is ``current_message_for_llm``),
   raising TypeError for EVERY backend and silently generating zero chat
   candidates.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance import chooser_tasks
from fighthealthinsurance.chooser_tasks import (
    _generate_appeal_candidates,
    _generate_chat_candidates,
    _select_candidate_models,
)
from fighthealthinsurance.models import ChooserCandidate, ChooserTask

APPEAL_TEXT = (
    "Dear Insurance Company, I am writing to appeal the denial of coverage "
    "for my patient. The requested procedure is medically necessary given the "
    "documented diagnosis and history. " * 2
)

CHAT_TEXT = (
    "You should start by requesting the full denial letter and your plan's "
    "appeal instructions; I can help you draft the appeal."
)

SCENARIO_TEXT = (
    "Procedure: MRI of lumbar spine\n"
    "Diagnosis: chronic lower back pain\n"
    "Insurance Company: Acme Health Assurance\n"
    "Denial Reason: The plan determined the imaging was not medically necessary."
)

CONVERSATION_TEXT = (
    "USER: My MRI claim was denied by my insurer.\n"
    "ASSISTANT: I can help you appeal that denial.\n"
    "USER: What documents do I need to get started?"
)


class FakeModel:
    """Mirrors the RemoteModelLike surface the chooser relies on, with the
    real generate_chat_response parameter names — so a caller regression back
    to ``current_message=`` fails loudly here."""

    def __init__(self, name, external=False, appeal_text=APPEAL_TEXT):
        self.name = name
        self._external = external
        self._appeal_text = appeal_text
        self.chat_calls = 0

    def __str__(self):
        return self.name

    @property
    def external(self):
        return self._external

    @property
    def context_only(self):
        return False

    def quality(self):
        return 100

    async def _infer_no_context(self, system_prompts, prompt, **kwargs):
        return self._appeal_text

    async def generate_chat_response(
        self,
        current_message_for_llm,
        previous_context_summary=None,
        history=None,
        temperature=0.7,
        is_professional=True,
        is_logged_in=True,
        is_medicaid_related=None,
    ):
        self.chat_calls += 1
        return (CHAT_TEXT, "summary")


class ScenarioModel(FakeModel):
    """First model in the generation list; returns parseable scenario text."""

    def __init__(self, scenario):
        super().__init__("fhi-scenario", external=False)
        self._scenario = scenario

    async def _infer_no_context(self, system_prompts, prompt, **kwargs):
        return self._scenario


class TestSelectCandidateModels:
    def _fake(self, name, external):
        return FakeModel(name, external=external)

    def test_dedupes_by_name(self):
        a = self._fake("fhi-2025", False)
        dup = self._fake("fhi-2025", False)
        b = self._fake("anthropic/claude-sonnet-4-6", True)
        selected = _select_candidate_models([a, dup, b], 4)
        assert [m.name for m in selected].count("fhi-2025") == 1
        assert {m.name for m in selected} == {
            "fhi-2025",
            "anthropic/claude-sonnet-4-6",
        }

    def test_mixes_internal_and_external(self):
        internals = [self._fake(f"fhi-{i}", False) for i in range(4)]
        externals = [
            self._fake("anthropic/claude-sonnet-4-6", True),
            self._fake("azure-openai/gpt-5", True),
        ]
        selected = _select_candidate_models(internals + externals, 4)
        assert len(selected) == 4
        # A plain models[:4] slice would have been internal-only; the selector
        # must reserve alternating slots for external backends.
        assert sum(1 for m in selected if m.external) == 2
        assert sum(1 for m in selected if not m.external) == 2

    def test_all_external_when_no_internal(self):
        externals = [
            self._fake("anthropic/claude-opus-4-8", True),
            self._fake("groq/llama-3.3-70b-versatile", True),
        ]
        selected = _select_candidate_models(externals, 4)
        assert {m.name for m in selected} == {
            "anthropic/claude-opus-4-8",
            "groq/llama-3.3-70b-versatile",
        }

    def test_respects_limit(self):
        models = [self._fake(f"m-{i}", i % 2 == 0) for i in range(10)]
        assert len(_select_candidate_models(models, 3)) == 3


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestAppealCandidatesUseExternalModels:
    async def test_external_models_generate_candidates_with_registry_names(self):
        task = await ChooserTask.objects.acreate(
            task_type="appeal", status="QUEUED", source="synthetic"
        )
        scenario = ScenarioModel(SCENARIO_TEXT)
        internal = FakeModel("fhi-2025-nov", external=False)
        claude = FakeModel("anthropic/claude-sonnet-4-6", external=True)
        gpt = FakeModel("azure-openai/gpt-5", external=True)

        text_backends = MagicMock(return_value=[scenario, internal, claude, gpt])
        with patch.object(
            chooser_tasks.ml_router, "generate_text_backends", text_backends
        ), patch.object(
            chooser_tasks,
            "_maybe_add_synthesized_candidate",
            new=AsyncMock(),
        ):
            await _generate_appeal_candidates(task)

        # The router must be asked for external models (synthetic data only).
        for call in text_backends.call_args_list:
            assert call.kwargs.get("use_external") is True or (
                call.args and call.args[0] is True
            )

        names = set(
            [
                c.model_name
                async for c in ChooserCandidate.objects.filter(
                    task=task, kind="appeal_letter"
                )
            ]
        )
        # External backends produced persisted candidates under their friendly
        # registry names — exactly what the usage dashboard aggregates.
        assert "anthropic/claude-sonnet-4-6" in names
        assert "azure-openai/gpt-5" in names
        await task.arefresh_from_db()
        assert task.num_candidates_generated >= 2

    async def test_no_models_leaves_task_without_candidates(self):
        task = await ChooserTask.objects.acreate(
            task_type="appeal", status="QUEUED", source="synthetic"
        )
        with patch.object(
            chooser_tasks.ml_router,
            "generate_text_backends",
            MagicMock(return_value=[]),
        ):
            await _generate_appeal_candidates(task)
        assert await ChooserCandidate.objects.filter(task=task).acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestChatCandidatesSignatureAndExternalModels:
    async def test_chat_candidates_generated_with_real_signature(self):
        """The chooser must call generate_chat_response with
        current_message_for_llm — with the old current_message= keyword this
        produced zero candidates (TypeError on every backend)."""
        task = await ChooserTask.objects.acreate(
            task_type="chat", status="QUEUED", source="synthetic"
        )
        scenario = ScenarioModel(CONVERSATION_TEXT)
        internal = FakeModel("fhi-2025-nov", external=False)
        claude = FakeModel("anthropic/claude-opus-4-8", external=True)

        chat_backends = MagicMock(return_value=[internal, claude])
        with patch.object(
            chooser_tasks.ml_router,
            "generate_text_backends",
            MagicMock(return_value=[scenario]),
        ), patch.object(
            chooser_tasks.ml_router, "get_chat_backends", chat_backends
        ), patch.object(
            chooser_tasks,
            "_maybe_add_synthesized_candidate",
            new=AsyncMock(),
        ):
            await _generate_chat_candidates(task)

        assert chat_backends.call_args.kwargs.get("use_external") is True

        candidates = [
            c
            async for c in ChooserCandidate.objects.filter(
                task=task, kind="chat_response"
            )
        ]
        names = {c.model_name for c in candidates}
        assert "anthropic/claude-opus-4-8" in names
        assert "fhi-2025-nov" in names
        assert internal.chat_calls >= 1
        assert claude.chat_calls >= 1

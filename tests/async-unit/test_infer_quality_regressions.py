"""Regressions for quiet quality-degradation bugs in the inference path.

Three bugs found by local end-to-end testing against a fake model backend:

- the non-dual backup fallback in ``_infer`` dropped ``ml_citations_context``
  and ``history``, so an appeal served by the backup lost its citations
  context and a chat turn served by the backup lost the whole conversation;
- ``_checked_infer`` guarded the plan/pubmed URL extraction on the TYPE OF
  patient_context (copy-paste), so with ``patient_context=None`` the URLs the
  context provided were treated as hallucinated by ``url_fixer`` and stripped
  from the appeal whenever network validation failed;
- ``get_system_prompts`` switched every prompt type to the ``*_not_patient``
  key under prof_pov, sending medically_necessary (no such variant) through
  the fallback that instructs the model to write an entire appeal letter.
"""

from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike, RemoteOpenLike


def _model(**kwargs) -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://primary.test/v1", "tok", "test-model", **kwargs)


class TestBackupFallbackForwardsFullContext:
    @pytest.mark.asyncio
    async def test_backup_call_keeps_citations_context_and_history(self):
        m = _model(backup_api_base="http://backup.test/v1")
        assert m.dual_mode is False
        history = [{"role": "user", "content": "earlier turn"}]
        citations = ["[1] Smith et al."]
        # Both legs fail (None) so the fallback definitely fires.
        infer_mock = AsyncMock(return_value=None)
        with patch.object(RemoteOpenLike, "_RemoteOpenLike__timeout_infer", infer_mock):
            result = await m._infer(
                system_prompts=["sys"],
                prompt="please appeal",
                ml_citations_context=citations,
                history=history,
            )
        assert result is None
        # Primary then backup.
        assert infer_mock.call_count == 2
        backup_kwargs = infer_mock.call_args_list[1].kwargs
        assert backup_kwargs["api_base"] == "http://backup.test/v1"
        assert backup_kwargs["ml_citations_context"] == citations
        assert backup_kwargs["history"] == history


class TestCheckedInferProtectsContextUrls:
    @pytest.mark.asyncio
    async def test_plan_context_urls_survive_without_patient_context(self):
        """URLs supplied via plan/pubmed context must be registered as input
        URLs even when patient_context is None -- otherwise url_fixer network-
        validates them and strips them on any validation failure."""
        m = _model()
        url = "https://example.com/coverage-policy.pdf"
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value=f"This service is covered per {url} and standard criteria."
        )
        # Simulate validation failing (offline / blocked): only URLs known
        # from the input survive url_fixer.
        with patch(
            "llm_result_utils.cleaner_utils.CleanerUtils.is_valid_url",
            new=lambda *a, **k: False,
        ):
            result = await m._checked_infer(
                prompt="denial text",
                patient_context=None,
                plan_context=f"Plan documents reference {url} for this service.",
                infer_type="medically_necessary",
                pubmed_context=None,
                system_prompt="sys",
                temperature=0.5,
            )
        assert result, "expected a (kind, text) result"
        assert url in result[0][1]

    @pytest.mark.asyncio
    async def test_pubmed_context_urls_survive_without_patient_context(self):
        """Same guarantee for the independent pubmed_context guard: its URLs
        must be registered as input URLs even when patient_context is None."""
        m = _model()
        url = "https://pubmed.ncbi.nlm.nih.gov/33500244/"
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value=f"Supported by the CGM outcomes study at {url} as cited."
        )
        with patch(
            "llm_result_utils.cleaner_utils.CleanerUtils.is_valid_url",
            new=lambda *a, **k: False,
        ):
            result = await m._checked_infer(
                prompt="denial text",
                patient_context=None,
                plan_context=None,
                infer_type="medically_necessary",
                pubmed_context=f"PubMed: relevant study {url} on CGM outcomes.",
                system_prompt="sys",
                temperature=0.5,
            )
        assert result, "expected a (kind, text) result"
        assert url in result[0][1]


class TestProfPovPromptSelection:
    def test_medically_necessary_keeps_its_own_prompt_under_prof_pov(self):
        m = _model()
        prompts = m.get_system_prompts("medically_necessary", prof_pov=True)
        assert prompts == m.system_prompts_map["medically_necessary"]

    def test_full_prof_pov_uses_not_patient_variant(self):
        m = _model()
        assert (
            m.get_system_prompts("full", prof_pov=True)
            == m.system_prompts_map["full_not_patient"]
        )

    def test_full_prof_pov_falls_back_when_map_lacks_variant(self):
        m = _model()
        m.system_prompts_map = {"full": ["patient prompt"]}
        prompts = m.get_system_prompts("full", prof_pov=True)
        assert len(prompts) == 1
        assert "healthcare professional" in prompts[0]

    def test_patient_pov_unchanged(self):
        m = _model()
        assert (
            m.get_system_prompts("full", prof_pov=False) == m.system_prompts_map["full"]
        )


class TestCheckedInferStripsThePreamble:
    """A chat-tuned model answers a letter request like a chat ("Sure, I can
    help you draft... Here's a simple one:"). On the appeal path that opening
    comes off before the draft is stored; on any other inference type the
    text is left as the model wrote it (the chat path never comes through
    here at all)."""

    PREAMBLE = (
        "Sure, I can help you draft a template for an appeal letter. "
        "Here's a simple one:"
    )
    LETTER = (
        "Dear Example Health,\n\n"
        "I am writing to appeal the denial of coverage for the physical therapy "
        "my physician ordered for my chronic lower back pain. The denial letter "
        "says the treatment is not medically necessary, but my records show six "
        "months of conservative care that did not relieve the pain, and my "
        "physician's notes explain why supervised therapy is the appropriate "
        "next step under the plan's own clinical criteria. I respectfully ask "
        "that you overturn the denial and approve the course of treatment.\n\n"
        "Sincerely,\nJane Doe\n"
    )

    @pytest.mark.asyncio
    async def test_the_appeal_path_starts_the_draft_at_the_letter(self):
        m = _model()
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value=f"{self.PREAMBLE}\n\n{self.LETTER}"
        )
        result = await m._checked_infer(
            prompt="denial text",
            patient_context=None,
            plan_context=None,
            infer_type="full",
            pubmed_context=None,
            system_prompt="sys",
            temperature=0.5,
        )
        assert result, "expected a (kind, text) result"
        text = result[0][1]
        assert text.startswith("Dear Example Health,")
        assert "Sure, I can help" not in text

    @pytest.mark.asyncio
    async def test_other_inference_types_keep_the_text_as_written(self):
        m = _model()
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value=f"{self.PREAMBLE}\n\n{self.LETTER}"
        )
        result = await m._checked_infer(
            prompt="denial text",
            patient_context=None,
            plan_context=None,
            infer_type="medically_necessary",
            pubmed_context=None,
            system_prompt="sys",
            temperature=0.5,
        )
        assert result, "expected a (kind, text) result"
        assert result[0][1].startswith(self.PREAMBLE)

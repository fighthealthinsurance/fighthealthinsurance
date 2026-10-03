"""Appeal prompt versions: what each full-letter call sends, and that every
draft records the version that wrote it."""

import itertools
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
)
from fighthealthinsurance.ml import appeal_prompt_versions as apv
from fighthealthinsurance.ml.ml_models import RemoteModelLike

LETTER = (
    "Dear Appeals Department,\n\n"
    + "I am writing to appeal the denial of my MRI, which my doctor ordered "
    "after six weeks of back pain that did not improve with therapy. " * 6
    + "\n\nSincerely,\n{{FIRST_NAME}} {{LAST_NAME}}"
)

# The September 2026 evaluation's system prompt, as its backends.json gives it.
EVAL_SYSTEM_PROMPT = (
    "You write health insurance appeal letters. Output ONLY the letter "
    "itself: no markdown formatting or headings, no commentary before or "
    "after, no notes to the user. Plain prose, and end immediately after the "
    "signature block."
)


class _RecordingBackend(RemoteModelLike):
    """A backend that writes the same letter and remembers each prompt."""

    def __init__(self):
        self.prompts = []

    async def _infer(self, *args, **kwargs):
        raise AssertionError("make_appeals should use the sync infer seam")

    def infer(
        self,
        prompt,
        patient_context,
        plan_context,
        infer_type,
        pubmed_context,
        ml_citations_context,
        prof_pov=False,
    ):
        self.prompts.append((infer_type, prompt))
        if infer_type == "full":
            return [("full", LETTER)]
        return [(infer_type, "it treats the condition the denial names")]


def _denial():
    return SimpleNamespace(
        denial_text="MRI denied as not medically necessary.",
        # Longer than three characters, or make_open_med_prompt asks nothing.
        procedure="Lumbar MRI",
        diagnosis="back pain",
        patient_user=None,
        primary_professional=None,
        qa_context=None,
        health_history=None,
        professional_to_finish=False,
        plan_id=None,
        claim_id=None,
        insurance_company="Acme Health",
        insurance_company_obj=None,
        plan_context=None,
        plan_documents_summary=None,
        use_external=False,
        denial_id=42,
    )


def _run(prompt_mode, template_generator=None, backends=("model-a", "model-b")):
    fakes = {name: _RecordingBackend() for name in backends}
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=lambda use_external=False: list(fakes),
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new={name: [fake] for name, fake in fakes.items()},
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        drafts = list(
            AppealGenerator().make_appeals(
                _denial(),
                template_generator
                or AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"]),
                medical_reasons=[],
                non_ai_appeals=[],
                prompt_mode=prompt_mode,
            )
        )
    return drafts, fakes


def test_the_contract_is_the_evaluations_text_word_for_word():
    assert EVAL_SYSTEM_PROMPT == (
        "You write health insurance appeal letters. " + apv.OUTPUT_CONTRACT
    )


def test_v1_leaves_the_prompt_alone():
    assert apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V1) == (
        "Write the appeal."
    )


def test_v2_puts_the_contract_last():
    prompt = apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V2)
    assert prompt.startswith("Write the appeal.")
    assert prompt.endswith(apv.OUTPUT_CONTRACT)


@pytest.mark.parametrize(
    "mode,draw,expected",
    [
        (apv.MODE_ORIGINAL, 0.1, apv.PROMPT_V1),
        (apv.MODE_NEW, 0.9, apv.PROMPT_V2),
        (apv.MODE_SPLIT, 0.2, apv.PROMPT_V2),
        (apv.MODE_SPLIT, 0.7, apv.PROMPT_V1),
        ("something-else", 0.2, apv.PROMPT_V1),
    ],
)
def test_choose_prompt_version(mode, draw, expected):
    with patch.object(apv, "_split_draw", lambda: draw):
        assert apv.choose_prompt_version(mode) == expected


def test_an_unreadable_setting_means_original():
    with patch(
        "fighthealthinsurance.models.LetterPromptMode.objects.order_by",
        side_effect=RuntimeError("database unavailable"),
    ):
        assert apv._read_mode() == apv.MODE_ORIGINAL


def test_original_mode_stamps_v1_and_sends_the_prompt_unchanged():
    drafts, fakes = _run(apv.MODE_ORIGINAL)
    full = [d for d in drafts if d.infer_type == "full"]
    assert full and {d.prompt_version for d in full} == {apv.PROMPT_V1}
    for fake in fakes.values():
        for infer_type, prompt in fake.prompts:
            assert apv.OUTPUT_CONTRACT not in prompt


def test_new_mode_sends_the_contract_and_stamps_v2():
    drafts, fakes = _run(apv.MODE_NEW)
    full = [d for d in drafts if d.infer_type == "full"]
    assert full and {d.prompt_version for d in full} == {apv.PROMPT_V2}
    sent = [p for fake in fakes.values() for t, p in fake.prompts if t == "full"]
    assert sent and all(p.endswith(apv.OUTPUT_CONTRACT) for p in sent)


def test_split_mode_draws_a_version_per_call_and_each_draft_keeps_its_own():
    # Calls are drawn in order: model-a then model-b, then the same two as
    # backups. Alternating gives model-a v2 and model-b v1 on both passes.
    draws = itertools.cycle([0.1, 0.9])
    with patch.object(apv, "_split_draw", lambda: next(draws)):
        drafts, fakes = _run(apv.MODE_SPLIT)
    by_model = {d.model_name: d.prompt_version for d in drafts if d.infer_type == "full"}
    assert by_model == {"model-a": apv.PROMPT_V2, "model-b": apv.PROMPT_V1}
    a_prompts = [p for t, p in fakes["model-a"].prompts if t == "full"]
    b_prompts = [p for t, p in fakes["model-b"].prompts if t == "full"]
    assert a_prompts and all(p.endswith(apv.OUTPUT_CONTRACT) for p in a_prompts)
    assert b_prompts and not any(apv.OUTPUT_CONTRACT in p for p in b_prompts)


def test_medically_necessary_drafts_carry_no_version_or_contract():
    # An empty template generator has no static letter, so make_appeals asks
    # each model the one-line medical-necessity question as well.
    drafts, fakes = _run(apv.MODE_NEW, AppealTemplateGenerator([], [], []))
    med_prompts = [
        p for fake in fakes.values() for t, p in fake.prompts if t != "full"
    ]
    assert med_prompts, "expected the medically-necessary calls to run"
    assert not any(apv.OUTPUT_CONTRACT in p for p in med_prompts)
    templated = [d for d in drafts if d.infer_type and d.infer_type != "full"]
    assert templated and all(d.prompt_version is None for d in templated)

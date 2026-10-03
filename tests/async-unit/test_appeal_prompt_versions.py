"""Appeal prompt versions: what each full-letter call sends, and that every
draft records the version that wrote it."""

import itertools
import threading
import time
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
    """A backend that writes the same letter and remembers each prompt.

    ``runts_first`` answers its first N full-letter calls with a scrap too
    short to be a letter, which sends make_appeals on to its backup pass.
    """

    def __init__(self, runts_first=0):
        self.prompts = []
        self.runts_first = runts_first

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
            full_calls = sum(1 for t, _p in self.prompts if t == "full")
            if full_calls <= self.runts_first:
                return [("full", "Too short.")]
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


def _run(
    prompt_mode,
    template_generator=None,
    backends=("model-a", "model-b"),
    runts_first=0,
):
    fakes = {name: _RecordingBackend(runts_first) for name in backends}
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


def test_v2_sends_exactly_the_v1_prompt_plus_the_contract():
    _drafts, v1_fakes = _run(apv.MODE_ORIGINAL)
    _drafts, v2_fakes = _run(apv.MODE_NEW)
    for name in v1_fakes:
        v1 = [p for t, p in v1_fakes[name].prompts if t == "full"]
        v2 = [p for t, p in v2_fakes[name].prompts if t == "full"]
        assert v1 and v2 == [p + "\n\n" + apv.OUTPUT_CONTRACT for p in v1]


def test_the_backup_pass_sends_and_stamps_the_version_too():
    # Every first-pass letter is a scrap, so make_appeals runs its backup
    # calls; those letters must also be written with, and stamped as, v2.
    drafts, fakes = _run(apv.MODE_NEW, runts_first=1)
    sent = [p for fake in fakes.values() for t, p in fake.prompts if t == "full"]
    assert len(sent) >= 4, "expected a first pass and a backup pass"
    assert all(p.endswith(apv.OUTPUT_CONTRACT) for p in sent)
    real = [d for d in drafts if d.infer_type == "full" and d.text == LETTER]
    assert real and {d.prompt_version for d in real} == {apv.PROMPT_V2}


def test_a_slow_read_never_blocks_other_letters():
    started, release = threading.Event(), threading.Event()

    def slow_read():
        started.set()
        release.wait(5)
        return apv.MODE_NEW

    cache = apv._ModeCache()
    with patch.object(apv, "_read_mode", slow_read):
        first = threading.Thread(target=cache.get)
        first.start()
        assert started.wait(5)
        began = time.monotonic()
        # While the first caller is stuck reading, a second one is answered
        # at once with the default instead of waiting behind it.
        assert cache.get() == apv.MODE_ORIGINAL
        assert time.monotonic() - began < 1.0
        release.set()
        first.join(5)
    assert cache.get() == apv.MODE_NEW

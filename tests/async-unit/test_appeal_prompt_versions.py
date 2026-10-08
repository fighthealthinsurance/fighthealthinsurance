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


def _denial(**overrides):
    fields = dict(
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
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _run(
    prompt_mode,
    template_generator=None,
    backends=("model-a", "model-b"),
    runts_first=0,
    denial=None,
    **make_appeals_kwargs,
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
                denial or _denial(),
                template_generator
                or AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"]),
                medical_reasons=[],
                non_ai_appeals=[],
                prompt_mode=prompt_mode,
                **make_appeals_kwargs,
            )
        )
    return drafts, fakes


def _full_prompts(fakes, name=None):
    """The full-letter prompts sent, to one model or to all of them."""
    chosen = [fakes[name]] if name else list(fakes.values())
    return [p for fake in chosen for t, p in fake.prompts if t == "full"]


def test_the_contract_is_the_evaluations_text_word_for_word():
    assert EVAL_SYSTEM_PROMPT == (
        "You write health insurance appeal letters. " + apv.OUTPUT_CONTRACT
    )


def test_v1_leaves_the_prompt_alone():
    assert apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V1) == (
        "Write the appeal."
    )


@pytest.mark.parametrize("version", [apv.PROMPT_V2, apv.PROMPT_V3])
def test_the_contract_versions_put_the_contract_last(version):
    prompt = apv.apply_prompt_version("Write the appeal.", version)
    assert prompt == "Write the appeal.\n\n" + apv.OUTPUT_CONTRACT


@pytest.mark.parametrize(
    "mode,draw,expected",
    [
        (apv.MODE_ORIGINAL, 0.1, apv.PROMPT_V1),
        (apv.MODE_NEW, 0.9, apv.PROMPT_V2),
        (apv.MODE_SPLIT, 0.2, apv.PROMPT_V2),
        (apv.MODE_SPLIT, 0.7, apv.PROMPT_V1),
        (apv.MODE_SECTIONED, 0.1, apv.PROMPT_V3),
        (apv.MODE_SECTIONED, 0.9, apv.PROMPT_V3),
        (apv.MODE_THIRDS, 0.0, apv.PROMPT_V1),
        (apv.MODE_THIRDS, 0.33, apv.PROMPT_V1),
        (apv.MODE_THIRDS, 0.34, apv.PROMPT_V2),
        (apv.MODE_THIRDS, 0.66, apv.PROMPT_V2),
        (apv.MODE_THIRDS, 0.67, apv.PROMPT_V3),
        (apv.MODE_THIRDS, 0.99, apv.PROMPT_V3),
        ("something-else", 0.2, apv.PROMPT_V1),
    ],
)
def test_choose_prompt_version(mode, draw, expected):
    with patch.object(apv, "_split_draw", lambda: draw):
        assert apv.choose_prompt_version(mode) == expected


@pytest.mark.parametrize("mode", [m for m, _label in apv.MODE_CHOICES])
def test_each_mode_draws_exactly_the_versions_it_lists(mode):
    drawn = set()
    for draw in (0.0, 0.2, 0.4, 0.6, 0.8, 0.999):
        with patch.object(apv, "_split_draw", lambda: draw):
            drawn.add(apv.choose_prompt_version(mode))
    assert drawn == set(apv.MODE_VERSIONS[mode])


def test_the_existing_modes_keep_their_values():
    # LetterPromptMode rows store these values; a renamed one would change
    # what history rows mean.
    assert (apv.MODE_ORIGINAL, apv.MODE_NEW, apv.MODE_SPLIT) == (
        "original",
        "new",
        "split",
    )
    assert apv.MODE_VERSIONS[apv.MODE_ORIGINAL] == (apv.PROMPT_V1,)
    assert apv.MODE_VERSIONS[apv.MODE_NEW] == (apv.PROMPT_V2,)
    assert apv.MODE_VERSIONS[apv.MODE_SPLIT] == (apv.PROMPT_V1, apv.PROMPT_V2)


def test_only_the_split_modes_draw_at_random():
    assert apv.RANDOM_MODES == {apv.MODE_SPLIT, apv.MODE_THIRDS}


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


# --- v3: the sectioned prompt ------------------------------------------------

DENIAL_TEXT = "Your MRI was denied as not medically necessary."
HINT_TAIL = "\n\n--- Denial-type guidance ---\nDenial-type hint."


def _sectioned(**overrides):
    kwargs = dict(
        denial_text=DENIAL_TEXT,
        procedure="MRI",
        diagnosis="back pain",
        professional_to_finish=True,
        professional="Dr. Jane Rivera, MD",
        plan_id="P-1",
        insurance_company="Acme Health",
        plan_context="Plan covers MRI after six weeks of conservative care.",
        pubmed_context="PMID 1: Example abstract.",
    )
    kwargs.update(overrides)
    prompt = AppealGenerator().make_sectioned_open_prompt(**kwargs)
    assert prompt is not None
    return prompt


def _written_with(prompt):
    """Which version a sent full-letter prompt reads as."""
    sectioned = prompt.startswith("TASK: ")
    contract = prompt.endswith("\n\n" + apv.OUTPUT_CONTRACT)
    if sectioned:
        return apv.PROMPT_V3 if contract else "sectioned without the contract"
    return apv.PROMPT_V2 if contract else apv.PROMPT_V1


def test_v3_lays_the_prompt_out_in_labelled_sections_in_order():
    prompt = _sectioned()
    labels = [
        "TASK: Write a health insurance appeal for procedure MRI with "
        "diagnosis back pain",
        "POINT OF VIEW:",
        "DETAILS TO INCLUDE",
        "PLAN DETAILS:",
        "CITATION INSTRUCTIONS:",
        "PubMed references (use these): PMID 1",
        "DENIAL LETTER:",
    ]
    positions = [prompt.find(label) for label in labels]
    assert -1 not in positions
    assert positions == sorted(positions)
    assert prompt.startswith(labels[0])
    assert prompt.endswith("DENIAL LETTER:\n" + DENIAL_TEXT)


def test_v3_has_no_example_openings():
    prompt = _sectioned()
    assert "GOOD EXAMPLE" not in prompt
    assert "Good phrases" not in prompt
    assert "Sign the letter as Dr. Jane Rivera, MD." in prompt


def test_v3_for_the_patient_voice_has_no_point_of_view_section():
    assert "POINT OF VIEW" not in _sectioned(professional_to_finish=False)


def test_v3_without_denial_text_is_no_prompt():
    assert AppealGenerator().make_sectioned_open_prompt(denial_text=None) is None


def test_sectioned_mode_sends_v3_with_the_contract_last_and_stamps_v3():
    drafts, fakes = _run(
        apv.MODE_SECTIONED, denial=_denial(professional_to_finish=True)
    )
    full = [d for d in drafts if d.infer_type == "full"]
    assert full and {d.prompt_version for d in full} == {apv.PROMPT_V3}
    sent = _full_prompts(fakes)
    assert sent and {_written_with(p) for p in sent} == {apv.PROMPT_V3}
    assert not any("GOOD EXAMPLE" in p for p in sent)


def test_v3_sends_the_sectioned_prompt_built_once_plus_the_contract():
    built = []
    real = AppealGenerator.make_sectioned_open_prompt

    def spy(self, **kwargs):
        built.append(real(self, **kwargs))
        return built[-1]

    with patch.object(AppealGenerator, "make_sectioned_open_prompt", spy):
        _drafts, fakes = _run(apv.MODE_SECTIONED)
    assert len(built) == 1
    sent = _full_prompts(fakes)
    assert sent and all(p == built[0] + "\n\n" + apv.OUTPUT_CONTRACT for p in sent)


@pytest.mark.parametrize("mode", [apv.MODE_ORIGINAL, apv.MODE_NEW, apv.MODE_SPLIT])
def test_modes_without_v3_never_build_the_sectioned_prompt(mode):
    with patch.object(AppealGenerator, "make_sectioned_open_prompt") as builder:
        _run(mode)
    builder.assert_not_called()


@pytest.mark.parametrize(
    "draws,expected",
    [
        ([0.9, 0.1], {"model-a": apv.PROMPT_V3, "model-b": apv.PROMPT_V1}),
        ([0.5, 0.9], {"model-a": apv.PROMPT_V2, "model-b": apv.PROMPT_V3}),
        ([0.1, 0.5], {"model-a": apv.PROMPT_V1, "model-b": apv.PROMPT_V2}),
    ],
)
def test_thirds_mode_draws_a_version_per_call_and_each_draft_keeps_its_own(
    draws, expected
):
    # Calls are drawn in order: model-a then model-b, then the same two as
    # backups, so the cycle gives each model the same version on both passes.
    cycle = itertools.cycle(draws)
    with patch.object(apv, "_split_draw", lambda: next(cycle)):
        drafts, fakes = _run(apv.MODE_THIRDS)
    by_model = {
        d.model_name: d.prompt_version for d in drafts if d.infer_type == "full"
    }
    assert by_model == expected
    for name, version in expected.items():
        sent = _full_prompts(fakes, name)
        assert sent and {_written_with(p) for p in sent} == {version}


def test_the_specialized_hint_goes_before_the_contract_in_v3():
    with patch.object(
        AppealGenerator, "_best_internal_model_name", return_value="model-a"
    ), patch.object(
        AppealGenerator,
        "_build_specialized_hint_block",
        return_value="Denial-type hint.",
    ):
        _drafts, fakes = _run(apv.MODE_SECTIONED, specialized_templates=[object()])
    hinted = [p for p in _full_prompts(fakes, "model-a") if "Denial-type hint." in p]
    assert len(hinted) == 1
    assert hinted[0].startswith("TASK: ")
    assert hinted[0].endswith(HINT_TAIL + "\n\n" + apv.OUTPUT_CONTRACT)


def test_the_backup_pass_sends_and_stamps_v3_too():
    drafts, fakes = _run(apv.MODE_SECTIONED, runts_first=1)
    sent = _full_prompts(fakes)
    assert len(sent) >= 4, "expected a first pass and a backup pass"
    assert {_written_with(p) for p in sent} == {apv.PROMPT_V3}
    real = [d for d in drafts if d.infer_type == "full" and d.text == LETTER]
    assert real and {d.prompt_version for d in real} == {apv.PROMPT_V3}


def test_a_shed_retry_rerenders_each_calls_own_layout():
    # model-a draws v3 and model-b v1. Every first-pass and backup letter is a
    # scrap, so make_appeals retries with context shed: the PubMed block comes
    # out of both prompts, and each call keeps its layout, its contract (or
    # none) and its version.
    cycle = itertools.cycle([0.9, 0.1])
    with patch.object(apv, "_split_draw", lambda: next(cycle)):
        drafts, fakes = _run(
            apv.MODE_THIRDS, runts_first=2, pubmed_context="PMID 1: Example abstract."
        )
    for name, version in (("model-a", apv.PROMPT_V3), ("model-b", apv.PROMPT_V1)):
        first, _backup, retry = _full_prompts(fakes, name)
        assert "PMID 1: Example abstract." in first
        assert "PMID 1: Example abstract." not in retry
        assert _written_with(retry) == version
    real = {
        d.model_name: d.prompt_version
        for d in drafts
        if d.infer_type == "full" and d.text == LETTER
    }
    assert real == {"model-a": apv.PROMPT_V3, "model-b": apv.PROMPT_V1}

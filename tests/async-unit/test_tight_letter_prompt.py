"""v4: an appeal letter asked for with the evaluation's tight prompt.

Under v4 the letter's system prompt starts with the evaluation's own system
prompt, word for word, and adds only what the app depends on: the voice, the
placeholders filled in after the letter is written, and the recipient. Every
other version keeps the backend's own prompt.
"""

from fighthealthinsurance.letter_openings import APPEAL_SALUTATION_INSTRUCTION
from fighthealthinsurance.ml import appeal_prompt_versions as apv
from fighthealthinsurance.ml.ml_models import (
    TIGHT_PATIENT_LETTER_PROMPT,
    TIGHT_PROFESSIONAL_LETTER_PROMPT,
    RemoteFullOpenLike,
)

# The September 2026 evaluation's system prompt, as its backends.json gives it.
EVAL_SYSTEM_PROMPT = (
    "You write health insurance appeal letters. Output ONLY the letter "
    "itself: no markdown formatting or headings, no commentary before or "
    "after, no notes to the user. Plain prose, and end immediately after the "
    "signature block."
)


def _model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://primary.test/v1", "tok", "test-model")


def test_the_tight_prompt_is_the_evaluations_text_word_for_word():
    assert apv.TIGHT_SYSTEM_PROMPT == EVAL_SYSTEM_PROMPT


def test_a_v4_patient_letter_gets_the_tight_prompt():
    prompts = _model().get_system_prompts("full", False, apv.PROMPT_V4)
    assert prompts == [TIGHT_PATIENT_LETTER_PROMPT]
    assert prompts[0].startswith(EVAL_SYSTEM_PROMPT)


def test_a_v4_professional_letter_gets_the_tight_prompt():
    prompts = _model().get_system_prompts("full", True, apv.PROMPT_V4)
    assert prompts == [TIGHT_PROFESSIONAL_LETTER_PROMPT]
    assert prompts[0].startswith(EVAL_SYSTEM_PROMPT)


def test_the_other_versions_keep_the_backends_own_prompt():
    m = _model()
    for version in (None, apv.PROMPT_V1, apv.PROMPT_V2, apv.PROMPT_V3):
        assert (
            m.get_system_prompts("full", False, version) == m.system_prompts_map["full"]
        )
        assert (
            m.get_system_prompts("full", True, version)
            == m.system_prompts_map["full_not_patient"]
        )


def test_v4_leaves_the_other_prompt_types_alone():
    m = _model()
    assert (
        m.get_system_prompts("questions", False, apv.PROMPT_V4)
        == m.system_prompts_map["questions"]
    )


def test_the_old_persona_is_not_in_the_tight_prompts():
    for prompt in (TIGHT_PATIENT_LETTER_PROMPT, TIGHT_PROFESSIONAL_LETTER_PROMPT):
        assert "patio11" not in prompt
        assert "extensive medical expertise" not in prompt
        assert "example starters" not in prompt


def test_a_v4_patient_letter_keeps_the_placeholders():
    for placeholder in ("{{FIRST_NAME}} {{LAST_NAME}}", "{{SCSID}}", "{{GPID}}"):
        assert placeholder in TIGHT_PATIENT_LETTER_PROMPT


def test_a_v4_patient_letter_is_in_the_first_person():
    assert "first person" in TIGHT_PATIENT_LETTER_PROMPT


def test_a_v4_professional_letter_refers_to_the_patient_in_the_third_person():
    assert "third person" in TIGHT_PROFESSIONAL_LETTER_PROMPT
    assert "{{Your Name}}" in TIGHT_PROFESSIONAL_LETTER_PROMPT


def test_both_v4_voices_name_the_recipient():
    assert TIGHT_PATIENT_LETTER_PROMPT.endswith(APPEAL_SALUTATION_INSTRUCTION)
    assert TIGHT_PROFESSIONAL_LETTER_PROMPT.endswith(APPEAL_SALUTATION_INSTRUCTION)


def test_tight_mode_writes_every_letter_with_v4():
    assert apv.choose_prompt_version(apv.MODE_TIGHT) == apv.PROMPT_V4
    assert apv.MODE_VERSIONS[apv.MODE_TIGHT] == (apv.PROMPT_V4,)
    assert apv.MODE_TIGHT not in apv.RANDOM_MODES


def test_v4_sends_the_original_user_turn():
    assert apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V4) == (
        "Write the appeal."
    )
    assert not apv.uses_sectioned_prompt(apv.PROMPT_V4)


def test_letters_still_default_to_original():
    assert apv.choose_prompt_version(apv.MODE_ORIGINAL) == apv.PROMPT_V1

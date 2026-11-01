"""v4 and v5: appeal letters asked for with the evaluation's tight prompt.

v4 and v5 send v2's and v3's user turns, each ending with the output
contract, under the evaluation's own system prompt, word for word, followed
only by what the app depends on: the voice, the placeholders filled in after
the letter is written, and the recipient. v1 to v3 keep the backend's own
prompt.
"""

import itertools
from unittest.mock import patch

from fighthealthinsurance.letter_openings import APPEAL_SALUTATION_INSTRUCTION
from fighthealthinsurance.ml import appeal_prompt_stats
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

TIGHT_VERSIONS = (apv.PROMPT_V4, apv.PROMPT_V5)


def _model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://primary.test/v1", "tok", "test-model")


def test_the_tight_prompt_is_the_evaluations_text_word_for_word():
    assert apv.TIGHT_SYSTEM_PROMPT == EVAL_SYSTEM_PROMPT


def test_v4_and_v5_patient_letters_get_the_tight_prompt():
    for version in TIGHT_VERSIONS:
        prompts = _model().get_system_prompts("full", False, version)
        assert prompts == [TIGHT_PATIENT_LETTER_PROMPT]
        assert prompts[0].startswith(EVAL_SYSTEM_PROMPT)


def test_v4_and_v5_professional_letters_get_the_tight_prompt():
    for version in TIGHT_VERSIONS:
        prompts = _model().get_system_prompts("full", True, version)
        assert prompts == [TIGHT_PROFESSIONAL_LETTER_PROMPT]
        assert prompts[0].startswith(EVAL_SYSTEM_PROMPT)


def test_v1_to_v3_keep_the_backends_own_prompt():
    m = _model()
    for version in (None, apv.PROMPT_V1, apv.PROMPT_V2, apv.PROMPT_V3):
        assert (
            m.get_system_prompts("full", False, version) == m.system_prompts_map["full"]
        )
        assert (
            m.get_system_prompts("full", True, version)
            == m.system_prompts_map["full_not_patient"]
        )


def test_the_tight_versions_leave_the_other_prompt_types_alone():
    m = _model()
    for version in TIGHT_VERSIONS:
        assert (
            m.get_system_prompts("questions", False, version)
            == m.system_prompts_map["questions"]
        )


def test_v4_sends_v2s_user_turn():
    assert apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V4) == (
        apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V2)
    )
    assert apv.apply_prompt_version("Write the appeal.", apv.PROMPT_V4).endswith(
        apv.OUTPUT_CONTRACT
    )
    assert not apv.uses_sectioned_prompt(apv.PROMPT_V4)


def test_v5_sends_v3s_sectioned_user_turn():
    assert apv.uses_sectioned_prompt(apv.PROMPT_V5)
    assert apv.apply_prompt_version("TASK: appeal.", apv.PROMPT_V5) == (
        apv.apply_prompt_version("TASK: appeal.", apv.PROMPT_V3)
    )
    assert apv.apply_prompt_version("TASK: appeal.", apv.PROMPT_V5).endswith(
        apv.OUTPUT_CONTRACT
    )


def test_only_v4_and_v5_use_the_tight_system_prompt():
    for version in (apv.PROMPT_V1, apv.PROMPT_V2, apv.PROMPT_V3, None):
        assert not apv.uses_tight_system_prompt(version)
    for version in TIGHT_VERSIONS:
        assert apv.uses_tight_system_prompt(version)


def test_the_old_persona_is_not_in_the_tight_prompts():
    for prompt in (TIGHT_PATIENT_LETTER_PROMPT, TIGHT_PROFESSIONAL_LETTER_PROMPT):
        assert "patio11" not in prompt
        assert "extensive medical expertise" not in prompt
        assert "example starters" not in prompt


def test_a_tight_patient_letter_keeps_the_placeholders():
    for placeholder in ("{{FIRST_NAME}} {{LAST_NAME}}", "{{SCSID}}", "{{GPID}}"):
        assert placeholder in TIGHT_PATIENT_LETTER_PROMPT


def test_a_tight_patient_letter_is_in_the_first_person():
    assert "first person" in TIGHT_PATIENT_LETTER_PROMPT


def test_a_tight_professional_letter_refers_to_the_patient_in_the_third_person():
    assert "third person" in TIGHT_PROFESSIONAL_LETTER_PROMPT
    assert "{{Your Name}}" in TIGHT_PROFESSIONAL_LETTER_PROMPT


def test_both_tight_voices_name_the_recipient():
    assert TIGHT_PATIENT_LETTER_PROMPT.endswith(APPEAL_SALUTATION_INSTRUCTION)
    assert TIGHT_PROFESSIONAL_LETTER_PROMPT.endswith(APPEAL_SALUTATION_INSTRUCTION)


def test_the_v4_or_v5_mode_draws_only_v4_and_v5():
    draws = itertools.cycle([i / 200 for i in range(200)])
    with patch.object(apv, "_split_draw", lambda: next(draws)):
        drawn = [apv.choose_prompt_version(apv.MODE_SPLIT_V4_V5) for _ in range(400)]
    assert set(drawn) == {apv.PROMPT_V4, apv.PROMPT_V5}
    assert drawn.count(apv.PROMPT_V4) == drawn.count(apv.PROMPT_V5)


def test_the_v4_or_v5_mode_is_a_random_draw_of_those_two():
    assert apv.MODE_VERSIONS[apv.MODE_SPLIT_V4_V5] == (apv.PROMPT_V4, apv.PROMPT_V5)
    assert apv.MODE_SPLIT_V4_V5 in apv.RANDOM_MODES
    assert apv.MODE_SPLIT_V4_V5 in {m for m, _label in apv.MODE_CHOICES}


def test_letters_still_default_to_original():
    assert apv.choose_prompt_version(apv.MODE_ORIGINAL) == apv.PROMPT_V1
    assert apv.choose_prompt_version("not-a-mode") == apv.PROMPT_V1


def test_the_head_to_head_includes_the_v4_v5_pair():
    assert (apv.PROMPT_V4, apv.PROMPT_V5) in appeal_prompt_stats.PAIRS
    assert set(appeal_prompt_stats.VERSIONS) >= set(TIGHT_VERSIONS)

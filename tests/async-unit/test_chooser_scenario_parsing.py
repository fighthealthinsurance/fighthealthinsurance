"""The chooser's scenario parsing: case facts and the appeal prompt built
from them. No database, no models."""

from unittest.mock import patch

from fighthealthinsurance.chooser_tasks import (
    CHOOSER_MAX_FACTS,
    _answers_from_response,
    _build_appeal_prompt,
    _denial_letter,
    _facts_from_response,
    _interview_prompt,
    _scenario_prompt,
    _simple_case_due,
)
from fighthealthinsurance.condition_rankings import SeedPair

_SCENARIO = (
    "Procedure: MRI of lumbar spine\n"
    "Diagnosis: chronic lower back pain\n"
    "Insurance Company: Fictional Mutual\n"
    "Denial Reason: not medically necessary.\n"
)


def test_facts_on_one_line_split_on_semicolons():
    text = _SCENARIO + (
        "Case Facts: Patient in their 40s; symptoms for 8 months; "
        "six weeks of physical therapy with no improvement; "
        "clinician documented radiating leg pain.\n"
    )
    assert _facts_from_response(text) == [
        "Patient in their 40s",
        "symptoms for 8 months",
        "six weeks of physical therapy with no improvement",
        "clinician documented radiating leg pain",
    ]


def test_facts_as_a_bullet_list_under_the_label():
    text = _SCENARIO + (
        "**Case Facts:**\n"
        "- Patient in their 50s\n"
        "* Symptoms for two years.\n"
        "1. Tried NSAIDs and physical therapy\n"
    )
    assert _facts_from_response(text) == [
        "Patient in their 50s",
        "Symptoms for two years",
        "Tried NSAIDs and physical therapy",
    ]


def test_facts_stop_at_the_next_labelled_field():
    text = (
        "Case Facts:\n- Patient in their 30s\n- Symptoms for a year\n"
        "Denial Reason: not medically necessary.\n"
    )
    assert _facts_from_response(text) == ["Patient in their 30s", "Symptoms for a year"]


def test_no_facts_label_means_no_facts():
    assert _facts_from_response(_SCENARIO) == []


def test_facts_are_deduped_and_capped():
    facts = "; ".join(f"fact {i}" for i in range(CHOOSER_MAX_FACTS + 4))
    text = _SCENARIO + f"Case Facts: fact 0; {facts}\n"
    parsed = _facts_from_response(text)
    assert len(parsed) == CHOOSER_MAX_FACTS
    assert len(set(parsed)) == CHOOSER_MAX_FACTS


def test_seeded_scenario_prompt_names_the_pair_and_asks_for_facts():
    prompt = _scenario_prompt(SeedPair("MRI", "Lower back pain", 12))
    assert "Procedure: MRI\n" in prompt
    assert "Diagnosis: Lower back pain\n" in prompt
    assert "Case Facts:" in prompt
    assert "copy them verbatim" in prompt


def test_unseeded_scenario_prompt_lets_the_writer_invent_the_pair():
    prompt = _scenario_prompt(None)
    assert "Procedure: [a specific real medical procedure name" in prompt
    assert "Case Facts:" in prompt


def test_appeal_prompt_lists_facts_and_evidence_and_forbids_inventing():
    prompt = _build_appeal_prompt(
        {
            "procedure": "MRI",
            "diagnosis": "Lower back pain",
            "facts": ["Patient in their 40s", "Eight months of symptoms"],
            "evidence": [
                {"pmid": "123", "title": "MRI for low back pain", "url": "u"},
                {"pmid": "", "title": "", "url": ""},
            ],
        }
    )
    assert "- Patient in their 40s" in prompt
    assert "- MRI for low back pain (PMID 123)" in prompt
    assert "the only studies you may cite" in prompt
    assert "if nothing on file fits, cite nothing" in prompt
    assert "Never invent a study" in prompt


def test_appeal_prompt_without_evidence_says_to_cite_nothing():
    prompt = _build_appeal_prompt({"procedure": "MRI", "diagnosis": "Lower back pain"})
    assert "Case facts" not in prompt
    assert "there is none on file, so cite no studies" in prompt


def test_appeal_prompt_gives_the_patients_answers_and_the_article_summaries():
    prompt = _build_appeal_prompt(
        {
            "procedure": "MRI",
            "diagnosis": "Lower back pain",
            "qa_pairs": [
                {
                    "question": "Has the patient tried physical therapy?",
                    "answer": "Yes, for eight weeks.",
                },
                {"question": "Unanswered?", "answer": ""},
            ],
            "evidence": [
                {
                    "pmid": "123",
                    "title": "MRI for low back pain",
                    "summary": "Imaging changed care in 30%.",
                },
            ],
        }
    )
    assert (
        "Q: Has the patient tried physical therapy?\nA: Yes, for eight weeks." in prompt
    )
    assert "Unanswered?" not in prompt
    assert (
        "- MRI for low back pain (PMID 123)\n  Summary: Imaging changed care in 30%."
        in prompt
    )
    assert "Use only the case facts and the patient's answers above" in prompt
    assert "claim no more for a study than its summary says" in prompt


def test_appeal_prompt_without_answers_relies_on_the_facts_alone():
    prompt = _build_appeal_prompt(
        {"procedure": "MRI", "facts": ["Patient in their 40s"]}
    )
    assert "patient's answers" not in prompt
    assert "Use only the case facts above" in prompt


def test_the_denial_letter_for_the_question_generators_names_the_case():
    letter = _denial_letter(
        {
            "procedure": "MRI",
            "diagnosis": "Lower back pain",
            "insurance_company": "Fictional Mutual",
            "denial_text_preview": "Not medically necessary.",
        }
    )
    assert letter == (
        "Fictional Mutual denied coverage of MRI for Lower back pain. "
        "Reason given: Not medically necessary."
    )


def test_interview_prompt_numbers_the_questions_and_the_answer_lines():
    prompt = _interview_prompt(
        {"procedure": "MRI", "facts": ["Patient in their 40s"]},
        ["Has the patient tried physical therapy?", "How long have symptoms lasted?"],
    )
    assert "- Patient in their 40s" in prompt
    assert "Q2: How long have symptoms lasted?" in prompt
    assert "A2: [the patient's answer to Q2]" in prompt
    assert "A3:" not in prompt


def test_answers_are_read_by_number_in_the_questions_order():
    questions = ["Tried physical therapy?", "Any imaging before?", "Still working?"]
    reply = (
        "**A2:** No imaging so far.\n"
        'A1: "Yes, eight weeks of it, with no change."\n'
        "A3: [the patient's answer to Q3]\n"
    )
    assert _answers_from_response(reply, questions) == [
        {
            "question": "Tried physical therapy?",
            "answer": "Yes, eight weeks of it, with no change.",
        },
        {"question": "Any imaging before?", "answer": "No imaging so far."},
    ]


def test_a_reply_in_another_shape_gives_no_answers():
    assert (
        _answers_from_response("I'd rather not say.", ["Tried physical therapy?"]) == []
    )


def test_facts_keep_their_leading_numbers():
    """ "45-55 years old" is a fact whose first word is a number, not a list
    marker; the label-markup strip used to leave "years old"."""
    text = _SCENARIO + (
        "Case Facts: 45-55 years old; 6 months of lower back pain; "
        "3 rounds of physical therapy failed; 2.5 cm disc protrusion on prior imaging.\n"
    )
    assert _facts_from_response(text) == [
        "45-55 years old",
        "6 months of lower back pain",
        "3 rounds of physical therapy failed",
        "2.5 cm disc protrusion on prior imaging",
    ]


def test_a_numbered_list_marker_is_still_stripped():
    text = "Case Facts:\n1. Patient in their 40s\n2) 6 months of symptoms\n10. Tried NSAIDs\n"
    assert _facts_from_response(text) == [
        "Patient in their 40s",
        "6 months of symptoms",
        "Tried NSAIDs",
    ]


def test_a_bulleted_fact_with_a_colon_is_a_fact_not_the_next_field():
    text = (
        "Case Facts:\n"
        "- Age range: 45-55\n"
        "- **Symptoms:** eight months of back pain\n"
        "- Tried PT, no improvement\n"
        "Denial Reason: not medically necessary.\n"
    )
    assert _facts_from_response(text) == [
        "Age range: 45-55",
        "Symptoms: eight months of back pain",
        "Tried PT, no improvement",
    ]


def test_simple_scenario_prompt_asks_for_an_everyday_case_in_so_many_words():
    prompt = _scenario_prompt(None, simple=True)

    assert "explicitly SIMPLE" in prompt
    assert "two or three short facts" in prompt
    assert "Case Facts: [2 or 3 short facts" in prompt
    # The writer picks the everyday pair; nothing about variety or rarity.
    assert "everyday procedure, test or medication" in prompt
    assert "Be creative and varied" not in prompt
    assert "Provide the following in EXACTLY this structured format" in prompt


def test_a_simple_scenario_prompt_gives_only_everyday_denial_reasons():
    simple = _scenario_prompt(None, simple=True)
    assert "experimental treatment" not in simple
    assert "out of network" not in simple
    assert "visit limit reached" in simple
    assert "experimental treatment" in _scenario_prompt(None)


def test_a_standard_scenario_prompt_keeps_the_standard_lines():
    """Unseeded and seeded, the standard prompt asks for what it always did,
    with none of the simple case's lines."""
    for prompt in (
        _scenario_prompt(None),
        _scenario_prompt(SeedPair("MRI", "Lower back pain", 12)),
    ):
        assert "Case Facts: [3 to 5 short facts" in prompt
        assert "Be creative and varied." in prompt
        assert "'experimental treatment'" in prompt
        assert "SIMPLE" not in prompt
        assert "2 or 3 short facts" not in prompt
        assert "Keep it ordinary" not in prompt


def test_a_mid_range_share_selects_only_the_draws_below_it():
    with patch("fighthealthinsurance.chooser_tasks.CHOOSER_SIMPLE_CASE_SHARE", 0.25):
        with patch(
            "fighthealthinsurance.chooser_tasks.random.random", return_value=0.1
        ):
            assert _simple_case_due()
        with patch(
            "fighthealthinsurance.chooser_tasks.random.random", return_value=0.4
        ):
            assert not _simple_case_due()


def test_the_simple_case_share_is_a_draw():
    with patch("fighthealthinsurance.chooser_tasks.CHOOSER_SIMPLE_CASE_SHARE", 0.0):
        assert not any(_simple_case_due() for _ in range(50))
    with patch("fighthealthinsurance.chooser_tasks.CHOOSER_SIMPLE_CASE_SHARE", 1.0):
        assert all(_simple_case_due() for _ in range(50))

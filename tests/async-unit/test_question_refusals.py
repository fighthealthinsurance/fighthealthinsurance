"""A model's refusal, explanation or fill-in template is never shown as an
intake question, and never cached for other people.

Live in October 2026: a denial of "Test" got two "questions", the model's
sentence saying it could not write any (with a "?" added) and its template
"[Question]?" with the hint "One way to answer: [Answer if available]". Four
cached generic sets held the same kind of text and were served to everyone
with the same procedure and diagnosis.
"""

import uuid
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.ml.question_parsing import (
    NO_QUESTIONS,
    is_junk_question,
    parse_appeal_questions,
)
from fighthealthinsurance.models import GenericQuestionGeneration

REFUSAL = (
    "Since the provided context does not contain specific medical details or a "
    'valid denial reason (the input provided was "test"), I cannot generate '
    "specific clinical questions. However, based on your instructions, here is "
    "the format I will use once specific medical context is provided:"
)


# --- The reply that reached the page, in each shape it can arrive in ---------


def test_the_live_refusal_and_template_give_no_questions():
    assert parse_appeal_questions(REFUSAL + "\n\n[Question]? [Answer if available]") is None


def test_the_refusal_with_a_bold_numbered_template_gives_no_questions():
    reply = REFUSAL + "\n\n1. **[Question]?** [Answer if available]"
    assert parse_appeal_questions(reply) is None


def test_the_refusal_on_one_line_gives_no_questions():
    assert parse_appeal_questions(REFUSAL + " [Question]? [Answer if available]") is None


def test_a_line_without_a_question_mark_is_not_made_into_a_question():
    assert parse_appeal_questions("This treatment is necessary\n") is None


def test_an_apology_that_asks_for_more_is_not_a_question():
    reply = "I'm sorry, but what procedure was denied? Please provide more details."
    assert parse_appeal_questions(reply) is None


# --- Saying there is nothing to ask ------------------------------------------


def test_no_questions_on_its_own_is_an_empty_list():
    assert parse_appeal_questions(NO_QUESTIONS) == []


def test_no_questions_in_bold_is_an_empty_list():
    assert parse_appeal_questions(f"**{NO_QUESTIONS}**.") == []


def test_no_questions_after_a_stray_think_tag_is_an_empty_list():
    assert parse_appeal_questions(f"</think>\n{NO_QUESTIONS}") == []


def test_no_questions_does_not_hide_real_questions_beside_it():
    reply = f"1. What is the patient's age? 45\n{NO_QUESTIONS}"
    assert parse_appeal_questions(reply) == [("What is the patient's age?", "45")]


# --- Real questions still come through ---------------------------------------


def test_a_question_after_an_intro_line_is_kept():
    reply = "Sure! Here are some questions:\n1. What is the patient's age?"
    assert parse_appeal_questions(reply) == [("What is the patient's age?", "")]


def test_a_question_that_asks_the_reader_to_share_is_kept():
    reply = "Could you share whether the patient has tried physical therapy?"
    assert parse_appeal_questions(reply) == [(reply, "")]


def test_a_question_with_a_bold_word_inside_is_kept_whole():
    reply = "1. What is the patient's **BMI**? 32"
    assert parse_appeal_questions(reply) == [("What is the patient's BMI?", "32")]


def test_a_bold_label_before_the_question_is_dropped():
    reply = "**Question:** Has the patient tried physical therapy? Yes"
    assert parse_appeal_questions(reply) == [
        ("Has the patient tried physical therapy?", "Yes")
    ]


def test_a_bold_note_is_not_a_question():
    reply = "**Note:** these depend on the plan\n1. What is the patient's age? 45"
    assert parse_appeal_questions(reply) == [("What is the patient's age?", "45")]


def test_citation_markers_are_removed_not_treated_as_placeholders():
    reply = (
        "Has the patient tried metformin as first-line therapy per ADA guidance[1]? Yes\n"
        "What is the patient's most recent A1c? 8.2 [2]"
    )
    assert parse_appeal_questions(reply) == [
        ("Has the patient tried metformin as first-line therapy per ADA guidance?", "Yes"),
        ("What is the patient's most recent A1c?", "8.2"),
    ]


def test_a_long_one_line_question_with_an_e_g_is_not_cut_to_a_fragment():
    reply = (
        "Has the patient participated in a structured weight loss program "
        "(e.g., Weight Watchers) for at least six months? Yes"
    )
    questions = parse_appeal_questions(reply)
    assert questions is not None
    assert all(q[:1].isalpha() for q, _ in questions)


# --- Suggested answers that are not answers ----------------------------------


def test_a_placeholder_answer_is_not_shown_as_a_hint():
    reply = "What is the patient's age? [Answer if available]"
    assert parse_appeal_questions(reply) == [("What is the patient's age?", "")]


def test_unknown_is_not_shown_as_a_hint():
    reply = "What is the patient's age? UNKNOWN"
    assert parse_appeal_questions(reply) == [("What is the patient's age?", "")]


def test_a_request_for_more_details_is_not_shown_as_a_hint():
    reply = "What procedure was denied? Please provide more details."
    assert parse_appeal_questions(reply) == [("What procedure was denied?", "")]


# --- The junk check on its own -------------------------------------------------


def test_a_template_is_junk():
    assert is_junk_question("[Question]?")


def test_the_live_refusal_is_junk():
    assert is_junk_question(REFUSAL + "?")


def test_a_real_question_is_not_junk():
    assert not is_junk_question("Was this a screening or diagnostic colonoscopy?")


# --- The prompt offers NO_QUESTIONS only where it is safe ----------------------


def _questions_model():
    model = MagicMock(spec=RemoteFullOpenLike)
    model._infer_no_context = AsyncMock(return_value="1. What is the patient's age?")
    model.get_system_prompts = MagicMock(return_value=["Test system prompt"])
    model.model = "test-model"
    return model


@pytest.mark.asyncio
async def test_the_prompt_offers_no_questions_when_there_is_denial_text():
    model = _questions_model()
    await RemoteFullOpenLike.get_appeal_questions(
        model, denial_text="Test", procedure=None, diagnosis=None
    )
    assert NO_QUESTIONS in model._infer_no_context.call_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_the_prompt_does_not_offer_no_questions_without_denial_text():
    """The generic and prior-auth calls have no denial text to judge, and
    they are where questions help most."""
    model = _questions_model()
    await RemoteFullOpenLike.get_appeal_questions(
        model, denial_text=None, procedure="mri", diagnosis="back pain"
    )
    assert NO_QUESTIONS not in model._infer_no_context.call_args.kwargs["prompt"]


def test_the_shared_system_prompt_does_not_offer_no_questions():
    model = RemoteFullOpenLike("http://questions.test/v1", "tok", "test-model")
    prompts = model.get_system_prompts("questions")
    assert not any(NO_QUESTIONS in p for p in prompts)


# --- Specific questions skip a denial too short to ask about ----------------


@pytest.mark.asyncio
async def test_a_four_letter_denial_with_no_history_asks_no_model():
    model = mock.AsyncMock()
    with mock.patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends",
        return_value=[model],
    ):
        result = await MLAppealQuestionsHelper.generate_specific_questions(
            denial_text="Test", patient_context=None, procedure=None, diagnosis=None
        )
    assert result is None
    model.get_appeal_questions.assert_not_called()


# --- The generic cache only shares clean, complete sets ----------------------


def _pair(procedure: str, diagnosis: str) -> tuple[str, str]:
    """Rows written from async tests leak across tests, so each test keys
    its own pair (see test_generic_model_caching.unique_pair)."""
    suffix = uuid.uuid4().hex[:8]
    return f"{procedure} {suffix}", f"{diagnosis} {suffix}" if diagnosis else ""


def _backends(questions):
    model = mock.AsyncMock()
    model.get_appeal_questions.return_value = questions
    model.quality = MagicMock(return_value=1)
    patches = [
        mock.patch(
            "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends",
            return_value=[model],
        ),
        mock.patch(
            "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends",
            return_value=[model],
        ),
    ]
    return model, patches


GOOD = [
    ("Has the patient tried physical therapy?", ""),
    ("How long has the pain lasted?", ""),
]


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_cached_refusal_is_not_served_and_a_clean_set_replaces_it():
    procedure, diagnosis = _pair("lumbar mri", "back pain")
    await GenericQuestionGeneration.objects.acreate(
        procedure=procedure,
        diagnosis=diagnosis,
        generated_questions=[[REFUSAL + "?", ""], ["[Question]?", ""]],
    )
    model, patches = _backends(GOOD)
    with patches[0], patches[1]:
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=procedure, diagnosis=diagnosis
        )
    assert result == GOOD
    model.get_appeal_questions.assert_called()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_set_with_junk_in_it_is_not_cached():
    procedure, diagnosis = _pair("knee mri", "knee pain")
    _, patches = _backends(GOOD + [("[Question]?", "")])
    with patches[0], patches[1]:
        await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=procedure, diagnosis=diagnosis
        )
    assert not await GenericQuestionGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_single_question_is_not_cached():
    procedure, diagnosis = _pair("hip mri", "hip pain")
    _, patches = _backends(GOOD[:1])
    with patches[0], patches[1]:
        await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=procedure, diagnosis=diagnosis
        )
    assert not await GenericQuestionGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_procedure_without_a_diagnosis_gets_questions_but_is_not_cached():
    procedure, _ = _pair("shoulder mri", "")
    _, patches = _backends(GOOD)
    with patches[0], patches[1]:
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=procedure, diagnosis=None
        )
    assert result == GOOD
    assert not await GenericQuestionGeneration.objects.filter(
        procedure=procedure, diagnosis=""
    ).aexists()


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_a_placeholder_procedure_asks_no_model():
    model, patches = _backends(GOOD)
    with patches[0], patches[1]:
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure="Test", diagnosis="back pain"
        )
    assert result is None
    model.get_appeal_questions.assert_not_called()

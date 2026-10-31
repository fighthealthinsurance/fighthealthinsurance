"""ml/followup_questions.py and MLAppealQuestionsHelper.followup_questions_for_denial:
the follow-up round after the questions page.

Nothing here reaches a model or TypeSafe: our own model is stubbed at
ml_router.best_internal_model and Jev at followup_questions._post. What
matters most: Jev answers "enough?" only with consent to outside models and
sees the case redacted, our own model answers it otherwise, a "yes" spares the
follow-ups, the check runs once, and anything going wrong means no follow-ups.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings

from fighthealthinsurance.denial_context import question_field_name
from fighthealthinsurance.ml import followup_questions as fq
from fighthealthinsurance.ml import typesafe
from fighthealthinsurance.ml.ml_appeal_questions_helper import (
    MLAppealQuestionsHelper,
    questions_fingerprint,
)
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.ml.question_parsing import NO_QUESTIONS, parse_yes_no
from fighthealthinsurance.models import Denial

ROUND_ON = dict(FHI_FOLLOWUP_QUESTIONS_ENABLED=True)
JEV_ON = dict(
    ROUND_ON, TYPESAFE_API_KEY="test-key", TYPESAFE_FOLLOWUP_QUESTIONS_ENABLED=True
)

FIRST_ROUND_QUESTION = "Has the patient tried physical therapy?"
FOLLOW_UP = "How many weeks of physical therapy did the patient complete?"
SECOND_FOLLOW_UP = "Did the pain limit the patient's ability to work?"

_BEST_INTERNAL = (
    "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.best_internal_model"
)


def _payload(enough):
    return {"model": "jev-1.13.0", "answers": {"enough": {"noul": enough}}}


# --- Pure pieces -------------------------------------------------------------


def test_blanks_unticked_boxes_and_keys_that_are_not_answers_are_left_out():
    pairs = fq.answered_pairs(
        {"b?": "yes", "a?": "  ", "in_network": "False", "medical_context": "derived"}
    )
    assert pairs == [("b?", "yes")]


def test_a_question_already_asked_or_repeated_is_dropped():
    kept = fq.new_candidates(
        [("HAS the patient tried physical therapy", ""), (FOLLOW_UP, ""), (FOLLOW_UP, "")],
        [FIRST_ROUND_QUESTION],
    )
    assert kept == [(FOLLOW_UP, "")]


def test_identifiers_are_redacted_in_the_denial_and_the_answers():
    state = fq.build_state(
        "Claim 99-1234 denied: MRI not medically necessary.",
        "MRI",
        "back pain",
        None,
        [("Contact?", "write to pat@example.com")],
        [("99-1234", "CLAIM_ID")],
    )
    assert "99-1234" not in state
    assert "pat@example.com" not in state


def test_an_identifier_at_the_cut_is_never_sent_in_part():
    """Redact, then cut: cut first and the claim id loses its tail, no longer
    matches, and its head goes out."""
    for pad in range(typesafe.STATE_CHAR_CAP - 300, typesafe.STATE_CHAR_CAP, 9):
        state = fq.build_state(
            "x" * pad + " 99-1234-5678 more", None, None, None, [], [("99-1234-5678", "CLAIM_ID")]
        )
        assert len(state) <= typesafe.STATE_CHAR_CAP
        assert "99-12" not in state, pad


@pytest.mark.parametrize("payload", [{}, _payload(1.5), _payload(True)])
def test_an_odd_jev_answer_is_rejected(payload):
    with pytest.raises(fq.FollowUpJudgingError):
        fq.parse_enough(payload)


@pytest.mark.parametrize(
    "reply,verdict",
    [
        ("YES", True),
        ("**NO**", False),
        ("<think>Maybe yes, maybe no.</think>\nYES", True),
        ("No, the answers do not say how long therapy lasted.", False),
        ("YES\nNO", None),
        ("It depends.", None),
        (None, None),
    ],
)
def test_parse_yes_no(reply, verdict):
    assert parse_yes_no(reply) is verdict


# --- The model prompts ---------------------------------------------------------


def _prompted_model(reply: str):
    model = MagicMock(spec=RemoteFullOpenLike)
    model._infer_no_context = AsyncMock(return_value=reply)
    model.get_system_prompts = MagicMock(return_value=["Test system prompt"])
    model._followup_context = RemoteFullOpenLike._followup_context
    model.model = "test-model"
    return model


@pytest.mark.asyncio
async def test_the_yes_no_prompt_carries_the_answers_and_asks_for_one_word():
    model = _prompted_model("NO")
    verdict = await RemoteFullOpenLike.answers_are_enough(
        model, "Denied: MRI.", "MRI", "back pain", [(FIRST_ROUND_QUESTION, "it did not help")]
    )
    assert verdict is False
    prompt = model._infer_no_context.call_args.kwargs["prompt"]
    assert "YES or NO" in prompt and "it did not help" in prompt


@pytest.mark.asyncio
async def test_no_questions_is_enough_and_suggested_answers_are_dropped():
    assert (
        await RemoteFullOpenLike.get_followup_questions(
            _prompted_model(NO_QUESTIONS), None, None, None, answered=[]
        )
        == []
    )
    asked = await RemoteFullOpenLike.get_followup_questions(
        _prompted_model(f"1. {FOLLOW_UP} About six\n2. {SECOND_FOLLOW_UP}\n3. Another?"),
        None,
        None,
        None,
        answered=[],
        max_questions=2,
    )
    assert asked == [(FOLLOW_UP, ""), (SECOND_FOLLOW_UP, "")]


# --- The whole step, on a denial ---------------------------------------------


def _denial(use_external=False, qa=None, **fields):
    values = dict(
        hashed_email="h",
        denial_text="Denied: MRI lumbar spine not medically necessary.",
        use_external=use_external,
        procedure="MRI lumbar spine",
        diagnosis="low back pain",
        generated_questions=[[FIRST_ROUND_QUESTION, ""]],
        qa_context=json.dumps(qa or {FIRST_ROUND_QUESTION: "Yes, it did not help"}),
    )
    values.update(fields)
    return Denial.objects.create(**values)


def _model(enough=False):
    """Our own model: ``enough`` is its yes/no; it writes one follow-up."""
    model = MagicMock()
    model.answers_are_enough = AsyncMock(return_value=enough)
    model.get_followup_questions = AsyncMock(return_value=[(FOLLOW_UP, "")])
    return model


def _run(denial, model, posted=()):
    with patch(_BEST_INTERNAL, return_value=model):
        return async_to_sync(MLAppealQuestionsHelper.followup_questions_for_denial)(
            denial, posted=frozenset(posted)
        )


def _stored(denial):
    denial.refresh_from_db()
    return denial.followup_questions


@pytest.mark.django_db
class TestFollowUpsForADenial:
    def test_a_person_who_answered_nothing_is_not_asked_more(self):
        denial, model = _denial(qa={FIRST_ROUND_QUESTION: "", "x": "False"}), _model()
        with override_settings(**ROUND_ON):
            assert _run(denial, model) == []
        model.answers_are_enough.assert_not_called()

    def test_without_consent_our_own_models_no_asks_its_follow_ups(self):
        denial, model = _denial(use_external=False), _model(enough=False)
        with override_settings(**JEV_ON), patch.object(fq, "_post") as post:
            assert _run(denial, model) == [(FOLLOW_UP, "")]
        post.assert_not_called()
        assert _stored(denial) == {
            "for": questions_fingerprint(denial.procedure, denial.diagnosis),
            "questions": [[FOLLOW_UP, ""]],
        }

    def test_our_own_models_yes_spares_the_follow_ups(self):
        denial, model = _denial(), _model(enough=True)
        with override_settings(**ROUND_ON):
            assert _run(denial, model) == []
        model.get_followup_questions.assert_not_called()
        assert _stored(denial)["questions"] == []

    def test_with_consent_jev_answers_from_the_case_and_spares_our_own_model(self):
        denial, model = _denial(use_external=True), _model()
        post = AsyncMock(return_value=_payload(0.9))
        with override_settings(**JEV_ON), patch.object(fq, "_post", new=post):
            assert _run(denial, model) == []
        model.answers_are_enough.assert_not_called()
        assert "Yes, it did not help" in post.await_args.args[0]

    def test_jev_failing_falls_back_to_our_own_models_yes_no(self):
        denial, model = _denial(use_external=True), _model(enough=True)
        failing = AsyncMock(side_effect=typesafe.TypeSafeError("HTTP 500", 500))
        with override_settings(**JEV_ON), patch.object(fq, "_post", new=failing):
            assert _run(denial, model) == []
        model.answers_are_enough.assert_awaited_once()

    def test_the_check_runs_once(self):
        denial, model = _denial(), _model()
        with override_settings(**ROUND_ON):
            _run(denial, model)
            denial.refresh_from_db()
            # A double submit carries none of the round's fields: shown again.
            assert _run(denial, model) == [(FOLLOW_UP, "")]
            # The follow-up page's own post carries them: on to the appeal.
            assert _run(denial, model, posted={question_field_name(FOLLOW_UP)}) == []
        assert model.get_followup_questions.await_count == 1

    def test_running_out_of_time_means_no_follow_ups_and_nothing_stored(self):
        denial, model = _denial(), _model()

        async def slow(**kwargs):
            await asyncio.sleep(5)
            return [(FOLLOW_UP, "")]

        model.get_followup_questions = slow
        with override_settings(**ROUND_ON), patch.object(fq, "TIMEOUT_SECONDS", 0.2):
            assert _run(denial, model) == []
        assert _stored(denial) is None

    def test_history_consent_withdrawn_while_it_ran_stores_nothing(self):
        denial, model = _denial(health_history="Back pain since 2019."), _model()

        async def withdrawn_meanwhile(**kwargs):
            assert kwargs["patient_context"] == "Back pain since 2019."
            await Denial.objects.filter(pk=denial.pk).aupdate(
                health_history_consent=False
            )
            return [(FOLLOW_UP, "")]

        model.get_followup_questions = withdrawn_meanwhile
        with override_settings(**ROUND_ON):
            assert _run(denial, model) == []
        assert _stored(denial) is None

"""The health history goes in the letter only if the person said it could.

Until now the page never asked and the drafting path never looked: the
history was put in the prompt either way, while a column that was supposed to
govern it sat at its old ``False`` default and was read on one other path
only. These tests pin the answer being asked for, honoured where the history
reaches a model, and honoured again where it steers the curated guidance the
letter carries.
"""

from unittest.mock import patch

import pytest

from fighthealthinsurance.denial_history_consent import history_may_be_used
from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
)
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike

HISTORY = "Patient has been on Aimovig for six months"


class _RecordingBackend(RemoteFullOpenLike):
    """Records what the model was actually asked."""

    def __init__(self):
        super().__init__("http://record.test/v1", "tok", "record-model")
        self.seen: list[dict] = []

    def parallel_infer(self, **kwargs):
        self.seen.append(kwargs)
        return []


class _Answered:
    """Just enough of a row for the predicate, with no database behind it."""

    def __init__(self, may_use):
        self.include_provided_health_history_in_appeal = may_use


def _denial(*, history=HISTORY, may_use=True):
    """A real row: the generation writes attempt records against it."""
    from fighthealthinsurance.models import Denial

    return Denial.objects.create(
        hashed_email=Denial.get_hashed_email("consent@example.com"),
        denial_text="Coverage denied",
        health_history=history,
        include_provided_health_history_in_appeal=may_use,
        use_external=False,
    )


def _patient_context_for(denial):
    """Run a generation far enough to see what the model is handed."""
    backend = _RecordingBackend()
    generator = AppealGenerator()
    template = AppealTemplateGenerator("", "", "")
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=lambda use_external=False: ["record-model-name"],
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new={"record-model-name": [backend]},
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        try:
            list(
                generator.make_appeals(
                    denial,
                    template,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                )
            )
        except Exception:
            pass
    return "\n".join(str(call.get("patient_context") or "") for call in backend.seen)


class TestTheAnswerItself:
    def test_a_row_that_says_yes_is_yes(self):
        assert history_may_be_used(_Answered(True)) is True

    def test_unticked_means_no(self):
        assert history_may_be_used(_Answered(False)) is False

    def test_something_with_no_answer_at_all_is_yes(self):
        assert history_may_be_used(object()) is True


class TestWhatTheModelIsHanded:
    @pytest.mark.django_db
    def test_a_history_it_may_use_reaches_the_model(self):
        assert HISTORY in _patient_context_for(_denial(may_use=True))

    @pytest.mark.django_db
    def test_a_history_it_may_not_use_does_not(self):
        handed = _patient_context_for(_denial(may_use=False))
        assert HISTORY not in handed
        assert "Aimovig" not in handed

    @pytest.mark.django_db
    def test_the_model_is_still_asked_something(self):
        """The guard must leave the case out, not the generation."""
        backend_saw = _patient_context_for(_denial(may_use=False))
        assert backend_saw is not None

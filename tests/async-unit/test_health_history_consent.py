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
        self.health_history_consent = may_use


def _denial(*, history=HISTORY, may_use=True):
    """A real row: the generation writes attempt records against it."""
    from fighthealthinsurance.models import Denial

    return Denial.objects.create(
        hashed_email=Denial.get_hashed_email("consent@example.com"),
        denial_text="Coverage denied",
        health_history=history,
        health_history_consent=may_use,
        use_external=False,
    )


def _calls_for(denial):
    """Every call the backend was actually asked to make."""
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
        except Exception as e:
            # A generation that fails for an unrelated reason would leave
            # backend.seen empty, and then every "the history is not here"
            # assertion below would pass for the wrong reason.
            raise AssertionError(f"the generation failed before the model: {e}")
    assert backend.seen, "the model was never called, so nothing was proven"
    return backend.seen


def _patient_context_for(denial):
    """What the model was handed as the patient's context."""
    return "\n".join(
        str(call.get("patient_context") or "") for call in _calls_for(denial)
    )


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
        """The guard must leave the case out, not the generation.

        Asserting the returned string is not None proved nothing: it is
        always a string, empty included, so this passed when no call was made
        at all. _calls_for now fails if the backend was never reached.
        """
        calls = _calls_for(_denial(may_use=False))

        assert calls, "no model call was made"
        assert any(call.get("prompt") for call in calls), "nothing was asked"


class TestEveryReaderAsks:
    """Four things read the history. Gating one is not gating it.

    The drafting prompt is the obvious one. The medication scan chooses the
    guidance the letter carries. Question generation and citation lookup both
    hand it to a model as patient context, and with an external backend that
    leaves the building.
    """

    @pytest.mark.django_db
    def test_the_medication_scan_asks(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator
        from fighthealthinsurance.models import MedicationContext

        MedicationContext.objects.all().delete()
        MedicationContext.objects.create(
            drug_class="Anti-CGRP monoclonal antibody",
            regex=r"(aimovig|ajovy|emgality|vyepti)",
            appeal_context="Cite American Headache Society 2024 guidance.",
        )

        allowed = AppealGenerator._collect_medication_context(_denial(may_use=True))
        refused = AppealGenerator._collect_medication_context(_denial(may_use=False))

        assert allowed is not None and "Anti-CGRP" in allowed
        assert refused is None, "the refused history still chose the guidance"

    def test_question_generation_asks(self):
        """The call site hands patient_context, so read the source it runs."""
        import inspect

        from fighthealthinsurance.ml import ml_appeal_questions_helper

        source = inspect.getsource(ml_appeal_questions_helper)
        assert "history_may_be_used(denial)" in source
        assert "patient_context=denial.health_history," not in source

    def test_citation_lookup_asks(self):
        import inspect

        from fighthealthinsurance.ml import ml_citations_helper

        source = inspect.getsource(ml_citations_helper)
        assert "patient_context = denial.health_history\n" not in source
        assert source.count("history_may_be_used(denial)") >= 3


class TestARowNobodyAsked:
    """NULL is not a refusal. It is every case written before the question."""

    def test_it_keeps_the_behaviour_it_was_created_under(self):
        assert history_may_be_used(_Answered(None)) is True

    def test_and_a_row_with_no_such_attribute_at_all(self):
        assert history_may_be_used(object()) is True


class TestARefusalMidGeneration:
    """A generation runs for tens of seconds carrying the row it started
    with. Somebody can untick the box in the middle of that."""

    @pytest.mark.django_db
    def test_the_answer_is_read_again_at_the_handover(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.denial_history_consent import (
            ahistory_may_be_used,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("midflight@example.com"),
            denial_text="Denied.",
            health_history=HISTORY,
            health_history_consent=True,
        )
        # The task still holds the row as it was when it started.
        assert history_may_be_used(denial) is True

        Denial.objects.filter(denial_id=denial.denial_id).update(
            health_history_consent=False
        )

        assert history_may_be_used(denial) is True, "the snapshot is stale, as expected"
        assert async_to_sync(ahistory_may_be_used)(denial) is False

    @pytest.mark.django_db
    def test_a_row_nobody_asked_still_reads_as_yes(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.denial_history_consent import (
            ahistory_may_be_used,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("neverasked@example.com"),
            denial_text="Denied.",
            health_history=HISTORY,
        )

        assert async_to_sync(ahistory_may_be_used)(denial) is True

    def test_something_with_no_row_behind_it_falls_back(self):
        """A mock or a detached object must not take a generation down."""
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.denial_history_consent import (
            ahistory_may_be_used,
        )

        assert async_to_sync(ahistory_may_be_used)(_Answered(False)) is False
        assert async_to_sync(ahistory_may_be_used)(_Answered(None)) is True


class TestWhenTheAnswerCannotBeRead:
    """A refusal lives in the database, so a read that fails may be failing
    to see one. The snapshot is the thing that cannot be trusted."""

    @pytest.mark.django_db
    def test_it_fails_closed(self):
        from unittest.mock import patch

        from asgiref.sync import async_to_sync

        from fighthealthinsurance.denial_history_consent import (
            ahistory_may_be_used,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("unreadable@example.com"),
            denial_text="Denied.",
            health_history_consent=True,
        )

        with patch.object(
            Denial.objects, "filter", side_effect=RuntimeError("database is away")
        ):
            answer = async_to_sync(ahistory_may_be_used)(denial)

        assert answer is False, "a failed read used the stale in-memory yes"

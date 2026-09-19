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
    def test_a_refusal_during_the_run_reaches_the_drafting_prompt(self):
        """The reader that writes the letter asks again too.

        A generation carries the row it started with for tens of seconds.
        The two async readers re-read; this is the one that matters most,
        and it runs in a worker thread where it cannot await.
        """
        from fighthealthinsurance.models import Denial

        denial = _denial(may_use=True)
        # The patient unticks the box while this run is in flight. The
        # object in hand still says yes.
        Denial.objects.filter(denial_id=denial.denial_id).update(
            health_history_consent=False
        )
        assert denial.health_history_consent is True, "the snapshot is stale"

        handed = _patient_context_for(denial)

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

        allowed = AppealGenerator._collect_medication_context(
            _denial(may_use=True), True
        )
        refused = AppealGenerator._collect_medication_context(
            _denial(may_use=False), False
        )

        assert allowed is not None and "Anti-CGRP" in allowed
        assert refused is None, "the refused history still chose the guidance"

    @pytest.mark.django_db
    def test_question_generation_asks(self):
        """Behavioural: what the generator is actually handed.

        Reading the source proved nothing. Changing the refusal branch from
        None to the history itself left both spelling assertions passing,
        and that change discloses the very thing this guards.
        """
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml import ml_appeal_questions_helper as helper
        from fighthealthinsurance.models import Denial

        seen = {}

        async def recorder(**kwargs):
            seen.update(kwargs)
            return []

        def run(may_use):
            seen.clear()
            denial = Denial.objects.create(
                hashed_email=Denial.get_hashed_email(f"q{may_use}@example.com"),
                denial_text="Denied an MRI.",
                health_history=HISTORY,
                health_history_consent=may_use,
            )
            with patch.object(
                helper.MLAppealQuestionsHelper,
                "generate_specific_questions",
                recorder,
            ), patch.object(
                helper.MLAppealQuestionsHelper,
                "generate_generic_questions",
                recorder,
            ):
                async_to_sync(
                    helper.MLAppealQuestionsHelper.generate_questions_for_denial
                )(denial, speculative=False)
            assert "patient_context" in seen, "the generator was never reached"
            return seen["patient_context"]

        assert run(True) == HISTORY
        assert run(False) is None

    @pytest.mark.django_db
    def test_citation_lookup_asks(self):
        """Behavioural, for the same reason as the one above.

        Counting spellings in the source proved nothing: the refusal branch
        could be changed to hand over the history and every string assertion
        still passed. This records what the citation backend is given.
        """
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml import ml_citations_helper as helper
        from fighthealthinsurance.models import Denial

        seen = {}

        class _Recorder:
            async def get_citations(self, **kwargs):
                seen.update(kwargs)
                return []

        def run(may_use):
            seen.clear()
            denial = Denial.objects.create(
                hashed_email=Denial.get_hashed_email(f"c{may_use}@example.com"),
                denial_text="Denied an MRI.",
                health_history=HISTORY,
                health_history_consent=may_use,
            )
            with patch.object(
                helper.ml_router,
                "full_find_citation_backends",
                return_value=[_Recorder()],
            ):
                async_to_sync(
                    helper.MLCitationsHelper.generate_specific_citations
                )(denial=denial)
            assert "patient_context" in seen, "the backend was never reached"
            return seen["patient_context"]

        assert run(True) == HISTORY
        assert run(False) is None


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


class TestACaseThatIsGone:
    """A deleted row is not an unanswered question.

    Both helpers read the column back, and a flat value list returns None
    for a row that does not exist and for a row whose answer is NULL. Those
    mean opposite things: the second is nobody asked, the first is a case
    that has been removed, most likely on request.
    """

    @pytest.mark.django_db
    def test_a_missing_row_is_treated_as_refused(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.denial_history_consent import (
            ahistory_may_be_used,
            history_may_be_used_now,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("deleted@example.com"),
            denial_text="Denied.",
            health_history=HISTORY,
            health_history_consent=True,
        )
        # The generation still holds the object; the case is gone.
        Denial.objects.filter(denial_id=denial.denial_id).delete()

        assert history_may_be_used_now(denial) is False
        assert async_to_sync(ahistory_may_be_used)(denial) is False

    @pytest.mark.django_db
    def test_a_row_that_exists_with_no_answer_is_still_yes(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.denial_history_consent import (
            ahistory_may_be_used,
            history_may_be_used_now,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("unasked@example.com"),
            denial_text="Denied.",
            health_history=HISTORY,
        )

        assert history_may_be_used_now(denial) is True
        assert async_to_sync(ahistory_may_be_used)(denial) is True


class TestOneDecisionForEverythingDerived:
    """Consent is read once per generation, not once per use.

    Two reads let it change in between, so the raw history came out of the
    prompt while the drug-class guidance that was chosen because of the
    history stayed in.
    """

    @pytest.mark.django_db
    def test_a_refused_history_steers_nothing_in_the_prompt(self):
        """The drug-class guidance is derived, so it is governed too.

        Checking the patient context alone left a way through: the drafting
        call could pass the scan a hardcoded yes, the history would still be
        absent from the patient context, and the guidance chosen out of the
        history would ride into the prompt anyway. The drug name here
        appears only in the history.
        """
        from fighthealthinsurance.models import MedicationContext

        MedicationContext.objects.all().delete()
        MedicationContext.objects.create(
            drug_class="Anti-CGRP monoclonal antibody",
            regex=r"(aimovig|ajovy|emgality|vyepti)",
            appeal_context="Cite American Headache Society 2024 guidance.",
        )

        everything = "\n".join(
            f"{call.get('prompt') or ''}\n{call.get('patient_context') or ''}"
            for call in _calls_for(_denial(may_use=False))
        )

        assert HISTORY not in everything
        assert "Aimovig" not in everything
        assert "Anti-CGRP" not in everything, (
            "the letter carries guidance chosen out of a history they asked "
            "us not to use"
        )
        assert "American Headache Society" not in everything

    @pytest.mark.django_db
    def test_a_whole_generation_asks_once(self):
        """The mechanism, not the signature.

        The reader is made to say yes once and no afterwards. If anything
        downstream asks a second time it gets the opposite answer, and the
        letter carries drug-class guidance that was chosen out of a history
        the prompt no longer contains.
        """
        from fighthealthinsurance import generate_appeal as ga
        from fighthealthinsurance.models import MedicationContext

        MedicationContext.objects.all().delete()
        MedicationContext.objects.create(
            drug_class="Anti-CGRP monoclonal antibody",
            regex=r"(aimovig|ajovy|emgality|vyepti)",
            appeal_context="Cite American Headache Society 2024 guidance.",
        )

        answers = [True]

        def one_yes_then_no(denial):
            return answers.pop(0) if answers else False

        denial = _denial(may_use=True)
        with patch.object(
            ga, "history_may_be_used_now", side_effect=one_yes_then_no
        ) as reader:
            calls = _calls_for(denial)

        assert reader.call_count == 1, (
            f"consent was read {reader.call_count} times in one generation, "
            "so two uses of the history can disagree"
        )

        everything = "\n".join(
            f"{call.get('prompt') or ''}\n{call.get('patient_context') or ''}"
            for call in calls
        )
        assert HISTORY in everything, "the one yes did not reach the prompt"
        assert "Anti-CGRP" in everything, "the one yes did not reach the scan"


class TestARefusalThatLandsWhileAWorkerRuns:
    """A run that started with a yes finishes after the no.

    Both helpers store what they produce on the row, and both read that
    store back ahead of the consent check on the next run. So a worker
    finishing after a refusal could put the material back and have it used,
    and clearing the caches at the moment of the click would not catch it.
    What a run produced under a consent that has since changed is kept
    neither on the row nor in the answer it returns.
    """

    @pytest.mark.django_db
    def test_questions_are_not_stored_or_returned(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml import ml_appeal_questions_helper as helper
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("midq@example.com"),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
            health_history_consent=True,
        )

        async def refuse_then_answer(**kwargs):
            # The person unticks the box while the model is answering.
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                health_history_consent=False
            )
            return [("How long on Aimovig?", "")]

        with patch.object(
            helper.MLAppealQuestionsHelper,
            "generate_specific_questions",
            refuse_then_answer,
        ), patch.object(
            helper.MLAppealQuestionsHelper,
            "generate_generic_questions",
            refuse_then_answer,
        ):
            answer = async_to_sync(
                helper.MLAppealQuestionsHelper.generate_questions_for_denial
            )(denial, speculative=False)

        denial.refresh_from_db()
        assert answer is None, "the run's questions were handed back anyway"
        assert denial.generated_questions is None, "a copy was left on the row"

    @pytest.mark.django_db
    def test_citations_are_not_stored_or_returned(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml import ml_citations_helper as helper
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("midc@example.com"),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
            health_history_consent=True,
        )

        class _RefusesMidRun:
            async def get_citations(self, **kwargs):
                await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                    health_history_consent=False
                )
                return ["Chosen because of the Aimovig history"]

        with patch.object(
            helper.ml_router,
            "full_find_citation_backends",
            return_value=[_RefusesMidRun()],
        ):
            answer = async_to_sync(
                helper.MLCitationsHelper.generate_citations_for_denial
            )(denial=denial, speculative=False)

        denial.refresh_from_db()
        assert answer == [], "the run's citations were handed back anyway"
        assert denial.ml_citation_context is None, "a copy was left on the row"


class TestARowNobodyHasBeenAskedIsNotARefusal:
    """NULL is the common case, and the guard has to match it.

    Every row created before the column existed holds NULL, so does every
    row whose caller never sent the field, and NULL means the history may be
    used. Written as ``__in=[True, None]`` the guard compiled to
    ``IN (True)``, because SQL NULL is not equal to anything and Django
    drops None out of an IN list, so the store matched no row: the work was
    thrown away on every run and the letter went out with no citations at
    all.
    """

    @pytest.mark.django_db
    def test_the_filter_matches_a_row_with_no_answer(self):
        from fighthealthinsurance.denial_history_consent import still_allowed
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("neverasked-filter@example.com"),
            denial_text="Denied.",
            health_history=HISTORY,
        )
        assert denial.health_history_consent is None

        rows = Denial.objects.filter(denial_id=denial.denial_id)

        assert rows.filter(still_allowed()).count() == 1
        Denial.objects.filter(denial_id=denial.denial_id).update(
            health_history_consent=False
        )
        assert rows.filter(still_allowed()).count() == 0
        Denial.objects.filter(denial_id=denial.denial_id).update(
            health_history_consent=True
        )
        assert rows.filter(still_allowed()).count() == 1

    @pytest.mark.django_db
    def test_citations_are_kept_for_a_row_nobody_asked(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml import ml_citations_helper as helper
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("neverasked-cites@example.com"),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
        )

        class _Backend:
            async def get_citations(self, **kwargs):
                return ["Chosen because of the Aimovig history"]

        with patch.object(
            helper.ml_router, "full_find_citation_backends", return_value=[_Backend()]
        ):
            answer = async_to_sync(
                helper.MLCitationsHelper.generate_citations_for_denial
            )(denial=denial, speculative=False)

        denial.refresh_from_db()
        assert answer, "the run's citations were thrown away"
        assert denial.ml_citation_context, "nothing was stored for a row nobody asked"

    @pytest.mark.django_db
    def test_candidate_questions_are_kept_for_a_row_nobody_asked(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml import ml_appeal_questions_helper as helper
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("neverasked-q@example.com"),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
        )

        async def answer(**kwargs):
            return [("How long on Aimovig?", "")]

        with patch.object(
            helper.MLAppealQuestionsHelper, "generate_specific_questions", answer
        ), patch.object(
            helper.MLAppealQuestionsHelper, "generate_generic_questions", answer
        ):
            questions = async_to_sync(
                helper.MLAppealQuestionsHelper.generate_questions_for_denial
            )(denial, speculative=True)

        denial.refresh_from_db()
        assert questions, "the speculative run's questions were thrown away"
        assert (
            denial.candidate_generated_questions
        ), "the warm cache was never filled for a row nobody asked"


class TestPromotingWhatTheSpeculativePassFound:
    """The promotion path writes too, so it is governed too.

    Candidate questions come off a pass that may well have read the
    history, and the object doing the promoting can be holding a copy a
    refusal has since cleared off the row. Claiming them without saying so
    put them back.
    """

    @pytest.mark.django_db
    def test_a_refusal_stops_the_promotion(self):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            claim_generated_questions,
            questions_fingerprint,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("promote@example.com"),
            denial_text="Denied an MRI.",
            procedure="MRI",
            diagnosis="Migraine",
            health_history=HISTORY,
            health_history_consent=False,
        )

        stored = async_to_sync(claim_generated_questions)(
            denial.denial_id,
            [("How long on Aimovig?", "")],
            questions_fingerprint(denial.procedure, denial.diagnosis),
            used_history=bool(denial.health_history),
        )

        denial.refresh_from_db()
        assert stored is None, "the promotion handed the questions back"
        assert denial.generated_questions is None, "they were written to the row"

    @pytest.mark.django_db
    def test_the_promotion_path_itself_says_so(self):
        """Through the code that promotes, not the claim it calls.

        The claim takes the decision as an argument and defaults it to
        False, so a call site that leaves it out skips the check entirely.
        Both outer call sites pass it now, and this drives one of them.
        """
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.common_view_logic import DenialCreatorHelper
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            questions_fingerprint,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("promotepath@example.com"),
            denial_text="Denied an MRI.",
            procedure="MRI",
            diagnosis="Migraine",
            health_history=HISTORY,
            health_history_consent=False,
            candidate_procedure="MRI",
            candidate_diagnosis="Migraine",
            candidate_generated_questions=[["How long on Aimovig?", ""]],
        )

        promoted = async_to_sync(DenialCreatorHelper._questions_already_on_the_row)(
            denial.denial_id
        )

        denial.refresh_from_db()
        assert promoted is None, "the candidate set was promoted after a refusal"
        assert (
            denial.generated_questions is None
        ), "a set chosen out of a refused history was written to the row"

    @pytest.mark.django_db
    def test_a_set_already_standing_is_still_handed_back(self):
        """A refusal governs the write, not the read.

        Somebody has already been shown these and may have answered them.
        Refusing to hand them back would strand those answers, and they are
        on the row either way until the refusal clears it.
        """
        from asgiref.sync import async_to_sync

        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            claim_generated_questions,
            questions_fingerprint,
        )
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("standing@example.com"),
            denial_text="Denied an MRI.",
            procedure="MRI",
            diagnosis="Migraine",
            health_history=HISTORY,
            health_history_consent=False,
        )
        fingerprint = questions_fingerprint(denial.procedure, denial.diagnosis)
        Denial.objects.filter(denial_id=denial.denial_id).update(
            generated_questions=[["What did the letter say?", ""]],
            generated_questions_for=fingerprint,
        )

        stood = async_to_sync(claim_generated_questions)(
            denial.denial_id,
            [("A different question", "")],
            fingerprint,
            used_history=True,
        )

        assert stood == [["What did the letter say?", ""]]

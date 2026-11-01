"""The regulator is looked for again once the person tells us about their plan.

A denial letter often never says what kind of plan it is, so neither the
fixture regexes nor the model reading the letter can say who regulates it.
The review page's plan answers (where the coverage comes from, the state,
the employer) often can: some settle it outright, and the rest go to the
model with the letter.
"""

from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.common_view_logic import (
    DenialCreatorHelper,
    FindNextStepsHelper,
)
from fighthealthinsurance.models import Denial, PlanSource, Regulator
from fighthealthinsurance.regulator_routing import RoutingAnswer

_QUESTIONS = (
    "fighthealthinsurance.common_view_logic.DenialCreatorHelper."
    "generate_appeal_questions"
)
_GENERATOR = "fighthealthinsurance.common_view_logic.appealGenerator"

ERISA_LETTER = "You may have the right to file a civil action under ERISA."
PLAIN_LETTER = "Your request for an MRI was denied as not medically necessary."


def _routing(regulator: str) -> RoutingAnswer:
    return RoutingAnswer(True, regulator, "unknown", "File the appeal.")


def _source(name: str) -> PlanSource:
    # The fixture names carry trailing spaces.
    return PlanSource.objects.get(name__startswith=name)


class RegulatorFromPlanTestBase(TestCase):
    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    def _make_denial(self, letter=PLAIN_LETTER, **fields) -> Denial:
        return Denial.objects.create(
            denial_text=letter,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email("plan@example.com"),
            **fields,
        )

    def _rerun(self, denial, **kwargs) -> str:
        return async_to_sync(DenialCreatorHelper.extract_set_regulator_from_plan)(
            denial.denial_id, **kwargs
        )


class ExtractSetRegulatorFromPlanTest(RegulatorFromPlanTestBase):
    @patch(_GENERATOR)
    def test_medicaid_in_a_known_state_links_the_states_agency(self, generator):
        generator.get_regulator_routing = AsyncMock()
        denial = self._make_denial(your_state="TX")
        denial.plan_source.set([_source("Medicaid")])
        self.assertEqual(self._rerun(denial), "found")
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "TX Medicaid")
        # Settled by the answers alone: the model is not asked.
        generator.get_regulator_routing.assert_not_awaited()

    @patch(_GENERATOR)
    def test_unsettled_answers_go_to_the_model_with_the_letter(self, generator):
        generator.get_regulator_routing = AsyncMock(
            return_value=_routing("Texas Department of Insurance")
        )
        denial = self._make_denial(your_state="TX", employer_name="Acme Widgets")
        denial.plan_source.set([_source("Employer -- Private")])
        self.assertEqual(self._rerun(denial), "found")
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "TX insurance department")
        sent = generator.get_regulator_routing.call_args.args[0]
        self.assertTrue(sent.startswith(PLAIN_LETTER))
        self.assertIn("Employer: Acme Widgets.", sent)
        self.assertIn("Employer -- Private", sent)
        self.assertIn("lives in Texas", sent)

    @patch(_GENERATOR)
    def test_a_regulator_the_letter_names_is_kept(self, generator):
        generator.get_regulator_routing = AsyncMock()
        denial = self._make_denial(
            ERISA_LETTER, regulator=Regulator.objects.get(alt_name="ERISA")
        )
        denial.plan_source.set([_source("Medicare Advantage")])
        self.assertEqual(self._rerun(denial), "cached")
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "ERISA")
        generator.get_regulator_routing.assert_not_awaited()

    @patch(_GENERATOR)
    def test_settled_answers_replace_an_inference_from_the_letter(self, generator):
        generator.get_regulator_routing = AsyncMock()
        # Linked by the model reading the letter; the letter doesn't name it.
        tdi = async_to_sync(DenialCreatorHelper._regulator_row_for)(
            "Texas Department of Insurance"
        )
        denial = self._make_denial(your_state="TX", regulator=tdi)
        denial.plan_source.set([_source("Medicare Regular")])
        self.assertEqual(self._rerun(denial), "found")
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "CMS")

    @patch(_GENERATOR)
    def test_an_inference_is_kept_when_the_model_has_nothing_better(
        self, generator
    ):
        generator.get_regulator_routing = AsyncMock(return_value=_routing("Unknown"))
        tdi = async_to_sync(DenialCreatorHelper._regulator_row_for)(
            "Texas Department of Insurance"
        )
        denial = self._make_denial(your_state="TX", regulator=tdi)
        denial.plan_source.set([_source("Employer -- Private")])
        self.assertEqual(self._rerun(denial), "cached")
        denial.refresh_from_db()
        self.assertEqual(denial.regulator, tdi)

    @patch(_GENERATOR)
    def test_a_corrected_answer_drops_the_old_answers_regulator(self, generator):
        generator.get_regulator_routing = AsyncMock(return_value=_routing("Unknown"))
        cms = Regulator.objects.get(alt_name="CMS")
        denial = self._make_denial(your_state="TX", regulator=cms)
        denial.plan_source.set([_source("Employer -- Private")])
        self._rerun(denial, prior_plan_sources=["Medicare Advantage"], prior_state="TX")
        denial.refresh_from_db()
        self.assertIsNone(denial.regulator)

    @patch(_GENERATOR)
    def test_nothing_to_go_on_asks_nobody(self, generator):
        generator.get_regulator_routing = AsyncMock()
        denial = self._make_denial()
        denial.plan_source.set([_source("Don't know")])
        self.assertEqual(self._rerun(denial), "nothing_found")
        generator.get_regulator_routing.assert_not_awaited()

    @patch(_GENERATOR)
    def test_no_model_is_nothing_found(self, generator):
        from fighthealthinsurance.generate_appeal import ExtractionUnavailable

        generator.get_regulator_routing = AsyncMock(
            side_effect=ExtractionUnavailable("no model")
        )
        denial = self._make_denial(your_state="TX")
        denial.plan_source.set([_source("Union")])
        self.assertEqual(self._rerun(denial), "nothing_found")
        denial.refresh_from_db()
        self.assertIsNone(denial.regulator)


    @patch(_GENERATOR)
    def test_a_letter_naming_erisa_beats_a_medicare_answer(self, generator):
        # The letter-only pass has not linked the ERISA match yet.
        generator.get_regulator_routing = AsyncMock()
        denial = self._make_denial(ERISA_LETTER, your_state="TX")
        denial.plan_source.set([_source("Medicare Advantage")])
        self._rerun(denial)
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "ERISA")

    @patch(_GENERATOR)
    def test_an_answer_corrected_while_the_model_reads_wins(self, generator):
        denial = self._make_denial(your_state="TX")
        denial.plan_source.set([_source("Union")])

        async def corrected_meanwhile(*args, **kwargs):
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                your_state="NY", state="NY"
            )
            return _routing("Texas Department of Insurance")

        generator.get_regulator_routing = AsyncMock(side_effect=corrected_meanwhile)
        self.assertEqual(self._rerun(denial), "nothing_found")
        denial.refresh_from_db()
        self.assertIsNone(denial.regulator)


class FindNextStepsRerunsRegulatorTest(RegulatorFromPlanTestBase):
    def _find_next_steps(self, denial, **overrides):
        kwargs = dict(
            denial_id=denial.denial_id,
            email="plan@example.com",
            semi_sekret="sekret",
            procedure="",
            diagnosis="",
            insurance_company="",
            plan_id="",
            claim_id="",
            denial_type=None,
            denial_date=None,
        )
        kwargs.update(overrides)
        with patch(_QUESTIONS):
            return FindNextStepsHelper.find_next_steps(**kwargs)

    @patch(_GENERATOR)
    def test_plan_answers_find_the_regulator_and_show_it(self, generator):
        generator.get_regulator_routing = AsyncMock()
        denial = self._make_denial()
        info = self._find_next_steps(
            denial, plan_source=[_source("Medicaid")], your_state="TX"
        )
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "TX Medicaid")
        shown = " ".join(f"{a} {b}" for a, b in info.outside_help_details)
        self.assertIn("Texas Health and Human Services Commission", shown)

    @patch(_GENERATOR)
    def test_an_unchanged_resubmit_does_not_ask_again(self, generator):
        generator.get_regulator_routing = AsyncMock(return_value=_routing("Unknown"))
        denial = self._make_denial()
        answers = dict(plan_source=[_source("Union")], your_state="TX")
        self._find_next_steps(denial, **answers)
        self._find_next_steps(denial, **answers)
        self.assertEqual(generator.get_regulator_routing.await_count, 1)

    @patch(_GENERATOR)
    def test_an_insurer_only_correction_asks_again(self, generator):
        generator.get_regulator_routing = AsyncMock(return_value=_routing("Unknown"))
        denial = self._make_denial()
        answers = dict(plan_source=[_source("Union")], your_state="TX")
        self._find_next_steps(denial, **answers)
        self._find_next_steps(denial, insurance_company="Aetna", **answers)
        self.assertEqual(generator.get_regulator_routing.await_count, 2)
        self.assertIn(
            "Insurance company: Aetna.",
            generator.get_regulator_routing.call_args.args[0],
        )

    @patch(_GENERATOR)
    def test_a_failed_rerun_does_not_break_the_page(self, generator):
        generator.get_regulator_routing = AsyncMock(side_effect=RuntimeError("boom"))
        denial = self._make_denial()
        info = self._find_next_steps(
            denial, plan_source=[_source("Union")], your_state="TX"
        )
        self.assertIsNotNone(info)
        denial.refresh_from_db()
        self.assertIsNone(denial.regulator)

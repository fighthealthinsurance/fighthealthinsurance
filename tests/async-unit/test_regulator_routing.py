"""The model side of "who regulates this plan, and is it time to go to them?"

The prompt and answer format are a contract with the training data in
healthinsurance-llm-internal (dataset_tools/regulator_routing.py); the parser
has to read exactly what that data teaches the model to write, and the name
resolution has to land on rows the outside-help page can show.
"""

from unittest.mock import AsyncMock

import pytest

from fighthealthinsurance import regulator_routing as rr
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike

ANSWER = (
    "Escalate now: Yes\n"
    "Regulator: Texas Department of Insurance\n"
    "Plan kind: marketplace_individual\n"
    "Next step: Ask the Texas Department of Insurance for an independent external\n"
    "review, and file a consumer complaint there at the same time."
)


class TestParseRoutingAnswer:
    def test_reads_the_four_lines(self):
        answer = rr.parse_routing_answer(ANSWER)
        assert answer == rr.RoutingAnswer(
            True,
            "Texas Department of Insurance",
            "marketplace_individual",
            "Ask the Texas Department of Insurance for an independent external "
            "review, and file a consumer complaint there at the same time.",
        )
        assert answer.regulator_known

    def test_unknown_is_an_answer_but_not_a_known_regulator(self):
        answer = rr.parse_routing_answer("Escalate now: No\nRegulator: Unknown")
        assert answer is not None
        assert not answer.escalate
        assert not answer.regulator_known
        assert answer.plan_kind is None

    def test_thinking_block_is_skipped_and_answer_tag_wins(self):
        reply = (
            "<thinking>Escalate now: No\nRegulator: Mars</thinking>"
            f"<answer>{ANSWER}</answer>"
        )
        assert rr.parse_routing_answer(reply).regulator == "Texas Department of Insurance"

    def test_markdown_bold_labels_are_tolerated(self):
        reply = "**Escalate now:** Yes\n**Regulator:** Unknown\n**Plan kind:** unknown"
        answer = rr.parse_routing_answer(reply)
        assert answer.escalate and answer.plan_kind == "unknown"

    def test_off_list_plan_kind_reads_as_none(self):
        answer = rr.parse_routing_answer(
            "Escalate now: Yes\nRegulator: X\nPlan kind: church_plan"
        )
        assert answer.plan_kind is None

    @pytest.mark.parametrize(
        "reply",
        [
            None,
            "",
            "You should probably call your state.",
            "Escalate now: maybe\nRegulator: X",
            "Escalate now: Yes\nRegulator:",
            "Regulator: X\nPlan kind: medicaid",
        ],
    )
    def test_anything_else_is_none(self, reply):
        assert rr.parse_routing_answer(reply) is None


class TestRegulatorDefaults:
    def test_federal_rows_resolve_to_the_fixture(self):
        ebsa = rr.regulator_defaults(
            "U.S. Department of Labor, Employee Benefits Security Administration (EBSA)"
        )
        assert ebsa.fixture_alt_name == "ERISA"
        assert rr.regulator_defaults("Centers for Medicare & Medicaid Services (CMS)").fixture_alt_name == "CMS"
        assert rr.regulator_defaults("California Department of Managed Health Care (DMHC)").fixture_alt_name == "CDMC"

    def test_fixture_names_and_alt_names_resolve_too(self):
        assert rr.regulator_defaults("ERISA").fixture_alt_name == "ERISA"
        assert rr.regulator_defaults("Federal Department of Labor").fixture_alt_name == "ERISA"
        assert rr.regulator_defaults("cms").fixture_alt_name == "CMS"

    def test_state_departments_come_from_state_help(self):
        texas = rr.regulator_defaults("Texas Department of Insurance")
        assert texas is not None
        assert texas.name == "Texas Department of Insurance"
        assert texas.alt_name == "TX insurance department"
        assert texas.website.startswith("http")
        assert texas.phone
        assert texas.fixture_alt_name is None

    def test_medicaid_agencies_come_from_state_help(self):
        medi_cal = rr.regulator_defaults("California Department of Health Care Services (Medi-Cal)")
        assert medi_cal is not None
        assert medi_cal.alt_name == "CA Medicaid"

    def test_case_and_punctuation_do_not_matter(self):
        assert rr.regulator_defaults("texas department of insurance.") is not None
        assert rr.regulator_defaults("U.S. Office of Personnel Management (OPM)").alt_name == "OPM"
        assert rr.regulator_defaults("opm").alt_name == "OPM"

    @pytest.mark.parametrize("name", ["Unknown", "unknown.", "", "The Galactic Senate"])
    def test_unknown_and_unrecognised_names_are_none(self, name):
        assert rr.regulator_defaults(name) is None

    def test_every_state_has_both_bodies(self):
        from fighthealthinsurance.state_help import load_state_help

        for state in load_state_help().values():
            assert rr.regulator_defaults(state.insurance_department.name) is not None, state.name
            assert rr.regulator_defaults(state.medicaid.agency_name) is not None, state.name


def _model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("http://primary.test/v1", "tok", "test-model")


class TestBackendGetRegulator:
    @pytest.mark.asyncio
    async def test_asks_with_the_training_prompt_and_parses(self):
        model = _model()
        model._infer_no_context = AsyncMock(return_value=ANSWER)  # type: ignore[method-assign]
        answer = await model.get_regulator_routing("Dear Member, your claim is denied.")
        assert answer.regulator == "Texas Department of Insurance"
        kwargs = model._infer_no_context.call_args.kwargs
        assert kwargs["prompt"].startswith(rr.ROUTING_PROMPT)
        assert kwargs["prompt"].endswith("Dear Member, your claim is denied.")
        assert kwargs["temperature"] <= 0.2
        assert kwargs["raise_on_unavailable"] is True

    @pytest.mark.asyncio
    async def test_get_regulator_is_the_name_or_none(self):
        model = _model()
        model._infer_no_context = AsyncMock(return_value=ANSWER)  # type: ignore[method-assign]
        assert await model.get_regulator("letter") == "Texas Department of Insurance"
        model._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value="Escalate now: No\nRegulator: Unknown"
        )
        assert await model.get_regulator("letter") is None
        model._infer_no_context = AsyncMock(return_value="I can't tell.")  # type: ignore[method-assign]
        assert await model.get_regulator("letter") is None

    @pytest.mark.asyncio
    async def test_empty_letter_is_not_sent(self):
        model = _model()
        model._infer_no_context = AsyncMock(return_value=ANSWER)  # type: ignore[method-assign]
        assert await model.get_regulator_routing("   ") is None
        model._infer_no_context.assert_not_called()

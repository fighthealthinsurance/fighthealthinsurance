from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Test the denial_base including the regexes from the initial fixtures.
from asgiref.sync import sync_to_async
from django.test import TestCase

from fighthealthinsurance.models import DenialTypes, PlanType, Regulator
from fighthealthinsurance.process_denial import (
    ProcessDenialCodes,
    ProcessDenialRegex,
)


# Class for testing the denial_base logic.
class TestDenialTypes(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def test_denial_base_loaded(self):
        assert DenialTypes.objects.count() > 0

    @pytest.mark.asyncio
    async def test_denial_base_matches_medical_necessity(self):
        # Get the global denial type checker and feed it "x is not medically necessary" and make sure it matches the denial type "MEDICAL_NECESSITY"
        denial_type_checker = ProcessDenialRegex()
        denial_type = await denial_type_checker.get_denialtype(
            "x is not medically necessary", None, None
        )
        expected_denial_type = await DenialTypes.objects.filter(
            name="Medically Necessary"
        ).aget()
        assert denial_type[0] == expected_denial_type

    # ------------------------------------------------------------------
    # ProcessDenialRegex — procedure / diagnosis / templates coverage
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_procedure_matches_asked_for_pattern(self):
        checker = ProcessDenialRegex()
        result = await checker.get_procedure(
            "The doctor asked for an MRI scan of the knee"
        )
        assert result is not None
        assert "MRI" in result

    @pytest.mark.asyncio
    async def test_get_procedure_returns_none_when_no_match(self):
        checker = ProcessDenialRegex()
        result = await checker.get_procedure("XYZ")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_diagnosis_matches_not_indicated_for(self):
        checker = ProcessDenialRegex()
        result = await checker.get_diagnosis(
            "This treatment is not indicated for chronic migraine"
        )
        assert result is not None
        assert "migraine" in result.lower()

    @pytest.mark.asyncio
    async def test_get_diagnosis_returns_none_when_no_match(self):
        checker = ProcessDenialRegex()
        result = await checker.get_diagnosis("XYZ")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_procedure_and_diagnosis_returns_tuple(self):
        checker = ProcessDenialRegex()
        procedure, diagnosis = await checker.get_procedure_and_diagnosis(
            "The doctor asked for an MRI scan. The MRI is not indicated for migraine headaches"
        )
        assert procedure is not None
        assert diagnosis is not None

    @pytest.mark.asyncio
    async def test_get_appeal_templates_matches_covid_vaccine(self):
        checker = ProcessDenialRegex()
        templates = await checker.get_appeal_templates(
            "Please cover my covid vaccine appointment", diagnosis=""
        )
        assert len(templates) >= 1
        assert any("COVID" in t.name for t in templates)

    @pytest.mark.asyncio
    async def test_get_appeal_templates_returns_empty_for_unrelated_text(self):
        checker = ProcessDenialRegex()
        templates = await checker.get_appeal_templates(
            "Totally unrelated denial text", diagnosis=""
        )
        assert templates == []

    @pytest.mark.asyncio
    async def test_get_plan_type_matches_created_plan(self):
        # Fixture PlanType rows have blank regexes (non-selective); create
        # two rows with explicit regexes so we can verify the positive match
        # and the negative_regex exclusion branch in isolation.
        await sync_to_async(PlanType.objects.create)(
            name="TestHMO", alt_name="Test HMO plan", regex="HMO-only-text"
        )
        await sync_to_async(PlanType.objects.create)(
            name="TestPPO",
            alt_name="Test PPO plan",
            regex="PPO-only-text",
            negative_regex="excluded-marker",
        )

        checker = ProcessDenialRegex()

        hmo_plans = await checker.get_plan_type("patient uses HMO-only-text plan")
        assert any(p.name == "TestHMO" for p in hmo_plans)
        assert not any(p.name == "TestPPO" for p in hmo_plans)

        ppo_excluded = await checker.get_plan_type(
            "patient uses PPO-only-text plan with excluded-marker"
        )
        assert not any(p.name == "TestPPO" for p in ppo_excluded)

        ppo_plans = await checker.get_plan_type("patient uses PPO-only-text plan")
        assert any(p.name == "TestPPO" for p in ppo_plans)

    @pytest.mark.asyncio
    async def test_get_regulator_matches_erisa_and_respects_negative(self):
        # Create a regulator with a non-empty negative_regex so we can verify
        # the exclusion branch without perturbing the fixture rows.
        await sync_to_async(Regulator.objects.create)(
            name="TestRegulator",
            website="https://example.test/",
            alt_name="TR",
            regex="watchdog-alpha",
            negative_regex="excluded-token",
        )

        checker = ProcessDenialRegex()

        matched = await checker.get_regulator(
            "Contact watchdog-alpha for regulator info"
        )
        assert any(r.name == "TestRegulator" for r in matched)

        excluded = await checker.get_regulator(
            "Contact watchdog-alpha but this is excluded-token"
        )
        assert not any(r.name == "TestRegulator" for r in excluded)

    # ------------------------------------------------------------------
    # ProcessDenialCodes — ICD-10 and CPT preventive-care classification
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_process_denial_codes_icd_block_description_preventive(self):
        processor = await sync_to_async(ProcessDenialCodes)()
        fake_tag = SimpleNamespace(
            block_description="Encounter for preventive screening"
        )

        with patch(
            "fighthealthinsurance.process_denial.icd10.find",
            return_value=fake_tag,
        ):
            result = await processor.get_denialtype(
                "Diagnosis Z00.00 noted on the claim", None, None
            )

        assert len(result) == 1
        assert result[0].name == "Preventive Care"

    @pytest.mark.asyncio
    async def test_process_denial_codes_preventive_diagnosis_csv_override(self):
        """Returns the preventive denial when the ICD code is in the
        preventive_diagnosis CSV override (even if block description does not
        contain 'preventive')."""
        processor = await sync_to_async(ProcessDenialCodes)()
        # Inject a known code into the CSV override; keeps the test hermetic
        # regardless of whether ./data/preventive_diagnosis.csv is present.
        processor.preventive_diagnosis = {"Z00.00": "general exam"}
        fake_tag = SimpleNamespace(block_description="Encounter for general exam")

        with patch(
            "fighthealthinsurance.process_denial.icd10.find",
            return_value=fake_tag,
        ):
            result = await processor.get_denialtype(
                "Diagnosis Z00.00 noted on the claim", None, None
            )

        assert len(result) == 1
        assert result[0].name == "Preventive Care"

    @pytest.mark.asyncio
    async def test_process_denial_codes_preventive_cpt_override(self):
        processor = await sync_to_async(ProcessDenialCodes)()
        # Inject a known CPT code into the CSV override. 99391 is a real
        # preventive medicine E/M code and matches the cpt_code_re
        # (\d{4}[A-Z0-9]) regex.
        processor.preventive_codes = {"99391": "preventive visit"}

        # No ICD codes in the text so we fall straight through to the CPT path;
        # no need to patch icd10.find.
        result = await processor.get_denialtype(
            "Procedure code 99391 was denied", None, None
        )

        assert len(result) == 1
        assert result[0].name == "Preventive Care"

    @pytest.mark.asyncio
    async def test_process_denial_codes_unrelated_text_returns_empty(self):
        processor = await sync_to_async(ProcessDenialCodes)()
        processor.preventive_diagnosis = {}
        processor.preventive_codes = {}

        result = await processor.get_denialtype(
            "This claim is denied for lack of prior authorization", None, None
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_process_denial_codes_icd_match_but_not_preventive(self):
        """Returns [] when an ICD-10 code matches but is neither a preventive
        block description nor in the preventive_diagnosis override."""
        processor = await sync_to_async(ProcessDenialCodes)()
        processor.preventive_diagnosis = {}
        processor.preventive_codes = {}
        # Block description deliberately omits the substring "preventive" so
        # the re.search("preventive", ...) check in get_denialtype does not
        # accidentally match.
        fake_tag = SimpleNamespace(block_description="Respiratory disease")

        with patch(
            "fighthealthinsurance.process_denial.icd10.find",
            return_value=fake_tag,
        ):
            result = await processor.get_denialtype(
                "Diagnosis J45.909 noted on the claim", None, None
            )

        assert result == []

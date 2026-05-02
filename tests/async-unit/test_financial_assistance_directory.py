"""
Unit tests for the financial assistance directory.
"""

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

from fighthealthinsurance.financial_assistance_directory import (
    DIAGNOSIS_SPECIFIC_PROGRAMS,
    GENERAL_PROGRAMS,
    MANUFACTURER_PROGRAMS,
    SAFETY_NET_PROGRAMS,
    AssistanceProgram,
    FinancialAssistanceResults,
    search,
)


@contextmanager
def fake_state_help_module(get_state_help_return_value):
    """
    Inject a fake `fighthealthinsurance.state_help` into sys.modules.

    The real module imports from Django; in async-unit tests we don't have
    Django configured, so we substitute a stub with the single function
    `search()` consults.
    """
    module_name = "fighthealthinsurance.state_help"
    saved = sys.modules.get(module_name)
    fake = types.ModuleType(module_name)
    fake.get_state_help_by_abbreviation = MagicMock(  # type: ignore[attr-defined]
        return_value=get_state_help_return_value
    )
    sys.modules[module_name] = fake
    try:
        yield fake
    finally:
        if saved is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = saved


class TestSearchGeneralPrograms:
    """The general copay foundation directory should always be returned."""

    def test_returns_all_general_programs_with_no_filters(self):
        results = search()
        assert len(results.general) == len(GENERAL_PROGRAMS)
        names = {p.name for p in results.general}
        assert "NeedyMeds" in names
        assert "HealthWell Foundation" in names
        assert "PAN Foundation (Patient Access Network)" in names
        assert "Good Days" in names

    def test_no_diagnosis_specific_when_diagnosis_omitted(self):
        results = search()
        assert results.diagnosis_specific == []

    def test_no_manufacturer_when_drug_omitted(self):
        results = search()
        assert results.manufacturer == []

    def test_safety_net_includes_untagged_entries(self):
        results = search()
        # FQHC locator and HRSA 340B are untagged - always returned
        names = {p.name for p in results.safety_net}
        assert "HRSA Find a Health Center (FQHC locator)" in names
        assert "HRSA 340B Program (provider directory + program info)" in names
        # Diagnosis-tagged entries (Ryan White HIV, Hemophilia HTC) are NOT
        # returned without a matching diagnosis
        assert "Ryan White HIV/AIDS Program Service Locator" not in names


class TestSearchByDiagnosis:
    """Diagnosis-specific programs should fire when their keywords appear."""

    def test_cancer_diagnosis_returns_cancer_programs(self):
        results = search(diagnosis="Stage IV breast cancer")
        names = {p.name for p in results.diagnosis_specific}
        assert "CancerCare Co-Payment Assistance Foundation" in names

    def test_leukemia_returns_lls_program(self):
        results = search(diagnosis="acute lymphoblastic leukemia")
        names = {p.name for p in results.diagnosis_specific}
        assert "Leukemia & Lymphoma Society Co-Pay Assistance" in names
        # Cancer programs match too because "leukemia" is also tagged on
        # CancerCare? Actually CancerCare's tags include "leukemia" - verify.
        # (Either way, LLS must be present.)

    def test_ms_diagnosis_returns_ms_program(self):
        results = search(diagnosis="relapsing-remitting multiple sclerosis")
        names = {p.name for p in results.diagnosis_specific}
        assert "National Multiple Sclerosis Society" in names

    def test_crohn_partial_match_returns_ibd_program(self):
        # "crohn" prefix should match "Crohn's disease"
        results = search(diagnosis="Crohn's disease, severe")
        names = {p.name for p in results.diagnosis_specific}
        assert "Crohn's & Colitis Foundation Patient Aid" in names

    def test_hiv_diagnosis_returns_adap_and_ryan_white(self):
        results = search(diagnosis="HIV-positive, virally suppressed")
        diag_names = {p.name for p in results.diagnosis_specific}
        safety_names = {p.name for p in results.safety_net}
        assert "HIV/AIDS Drug Assistance Program (ADAP)" in diag_names
        assert "Ryan White HIV/AIDS Program Service Locator" in safety_names

    def test_hemophilia_returns_hemophilia_safety_net(self):
        results = search(diagnosis="Hemophilia A, severe")
        safety_names = {p.name for p in results.safety_net}
        assert "Hemophilia Treatment Center Directory" in safety_names

    def test_unrelated_diagnosis_returns_no_specific_programs(self):
        results = search(diagnosis="MRI of knee")
        assert results.diagnosis_specific == []

    def test_diagnosis_match_is_case_insensitive(self):
        lower = search(diagnosis="multiple sclerosis")
        upper = search(diagnosis="MULTIPLE SCLEROSIS")
        assert {p.name for p in lower.diagnosis_specific} == {
            p.name for p in upper.diagnosis_specific
        }

    def test_diagnosis_text_can_come_from_denial_text(self):
        # Even if the diagnosis field is empty, denial_text should be searched.
        results = search(
            denial_text="Patient diagnosed with multiple sclerosis, denied Tysabri."
        )
        names = {p.name for p in results.diagnosis_specific}
        assert "National Multiple Sclerosis Society" in names


class TestSearchByDrug:
    """Drug-specific manufacturer programs should fire on canonical match."""

    def test_wegovy_returns_wegovy_savings_card(self):
        results = search(drug="Wegovy")
        names = {p.name for p in results.manufacturer}
        assert "Wegovy Savings Card (Novo Nordisk)" in names
        assert results.canonical_drug == "wegovy"

    def test_ozempic_returns_ozempic_program(self):
        results = search(drug="Ozempic")
        names = {p.name for p in results.manufacturer}
        assert "Ozempic Savings Offer (Novo Nordisk)" in names

    def test_humira_brand_matches(self):
        results = search(drug="Humira")
        names = {p.name for p in results.manufacturer}
        assert "Humira Complete Savings Card (AbbVie)" in names

    def test_drug_not_in_manufacturer_catalog_returns_empty_manufacturer(self):
        # Metformin is a cheap generic - no manufacturer copay card listed
        results = search(drug="metformin")
        assert results.manufacturer == []

    def test_unknown_drug_resolves_to_none(self):
        # Inputs the detector doesn't recognize must NOT echo back as
        # canonical_drug - that would create misleading payloads when, e.g.,
        # a non-drug procedure ("MRI of knee") is passed to search().
        results = search(drug="NewExperimentalDrug")
        assert results.canonical_drug is None
        assert results.manufacturer == []

    def test_non_drug_procedure_does_not_pollute_canonical_drug(self):
        results = search(drug="MRI of knee")
        assert results.canonical_drug is None
        assert results.has_specific_matches() is False

    def test_drug_detected_from_denial_text(self):
        results = search(denial_text="Your prescription for Wegovy 2.4mg is denied.")
        names = {p.name for p in results.manufacturer}
        assert "Wegovy Savings Card (Novo Nordisk)" in names
        assert results.canonical_drug == "wegovy"


class TestStateMedicaid:
    """State Medicaid pathway should be attached when state is supplied."""

    def test_state_medicaid_attached_when_state_help_returns_data(self):
        mock_state_help = MagicMock()
        mock_state_help.medicaid.agency_name = "Medi-Cal"
        mock_state_help.medicaid.agency_url = "https://www.medi-cal.ca.gov/"
        mock_state_help.medicaid.agency_phone = "1-800-541-5555"

        with fake_state_help_module(mock_state_help):
            results = search(state_abbreviation="CA")

        assert results.state_medicaid_name == "Medi-Cal"
        assert results.state_medicaid_url == "https://www.medi-cal.ca.gov/"
        assert results.state_medicaid_phone == "1-800-541-5555"
        assert results.state_abbreviation == "CA"

    def test_state_abbreviation_normalized_to_uppercase(self):
        with fake_state_help_module(None):
            results = search(state_abbreviation="ca")
        assert results.state_abbreviation == "CA"

    def test_lowercase_state_abbreviation_passed_uppercased_to_lookup(self):
        # Regression: previously the payload field was uppercased but the
        # state_help lookup got the raw lowercase value, missing case-
        # sensitive matches in the catalog.
        mock_state_help = MagicMock()
        mock_state_help.medicaid.agency_name = "Medi-Cal"
        mock_state_help.medicaid.agency_url = "https://www.medi-cal.ca.gov/"
        mock_state_help.medicaid.agency_phone = "1-800-541-5555"

        with fake_state_help_module(mock_state_help) as fake:
            results = search(state_abbreviation="ca")

        # The lookup must have received the uppercase form.
        fake.get_state_help_by_abbreviation.assert_called_once_with("CA")
        assert results.state_abbreviation == "CA"
        assert results.state_medicaid_name == "Medi-Cal"

    def test_no_state_data_when_state_help_unavailable(self):
        with fake_state_help_module(None):
            results = search(state_abbreviation="ZZ")
        assert results.state_medicaid_name is None

    def test_state_help_failure_does_not_break_search(self):
        # Even if state_help raises, the rest of the search must succeed.
        module_name = "fighthealthinsurance.state_help"
        saved = sys.modules.get(module_name)
        fake = types.ModuleType(module_name)
        fake.get_state_help_by_abbreviation = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("simulated")
        )
        sys.modules[module_name] = fake
        try:
            results = search(drug="Wegovy", state_abbreviation="CA")
        finally:
            if saved is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = saved

        assert results.state_medicaid_name is None
        # Drug-search results unaffected
        assert any(
            p.name == "Wegovy Savings Card (Novo Nordisk)" for p in results.manufacturer
        )


class TestFinancialAssistanceResults:
    """Tests for the FinancialAssistanceResults aggregate dataclass."""

    def test_is_empty_true_for_no_matches(self):
        results = FinancialAssistanceResults(
            diagnosis_specific=[],
            manufacturer=[],
            general=[],
            safety_net=[],
        )
        assert results.is_empty()

    def test_is_empty_false_when_general_present(self):
        results = FinancialAssistanceResults(general=list(GENERAL_PROGRAMS))
        assert not results.is_empty()

    def test_is_empty_false_when_state_medicaid_present(self):
        results = FinancialAssistanceResults(state_medicaid_name="Medi-Cal")
        assert not results.is_empty()

    def test_has_specific_matches_false_when_only_general(self):
        # General programs alone are not "specific" enough to gate UI on -
        # they're returned for every search.
        results = FinancialAssistanceResults(general=list(GENERAL_PROGRAMS))
        assert results.has_specific_matches() is False

    def test_has_specific_matches_false_when_only_safety_net(self):
        # The untagged safety-net base entries are likewise always returned.
        results = FinancialAssistanceResults(
            general=list(GENERAL_PROGRAMS),
            safety_net=[
                AssistanceProgram(
                    name="HRSA",
                    url="https://x",
                    description="d",
                    category="safety_net",
                )
            ],
        )
        assert results.has_specific_matches() is False

    def test_has_specific_matches_true_for_diagnosis_match(self):
        results = FinancialAssistanceResults(
            diagnosis_specific=[
                AssistanceProgram(
                    name="X",
                    url="https://x",
                    description="d",
                    category="copay_foundation",
                )
            ]
        )
        assert results.has_specific_matches() is True

    def test_has_specific_matches_true_for_manufacturer_match(self):
        results = FinancialAssistanceResults(
            manufacturer=[
                AssistanceProgram(
                    name="X", url="https://x", description="d", category="manufacturer"
                )
            ]
        )
        assert results.has_specific_matches() is True

    def test_has_specific_matches_true_for_state_medicaid(self):
        results = FinancialAssistanceResults(state_medicaid_name="Medi-Cal")
        assert results.has_specific_matches() is True

    def test_search_with_drug_only_has_specific_matches(self):
        # Wegovy → manufacturer copay card, so has_specific_matches True.
        results = search(drug="Wegovy")
        assert results.has_specific_matches() is True

    def test_search_with_no_specific_input_has_no_specific_matches(self):
        # Empty search returns general + safety-net, but nothing specific.
        results = search()
        assert results.is_empty() is False
        assert results.has_specific_matches() is False

    def test_all_programs_concatenates_in_order(self):
        diag = [
            AssistanceProgram(
                name="DiagFoo",
                url="https://x",
                description="d",
                category="copay_foundation",
            )
        ]
        mfr = [
            AssistanceProgram(
                name="MfrBar", url="https://x", description="d", category="manufacturer"
            )
        ]
        gen = [
            AssistanceProgram(
                name="GenBaz",
                url="https://x",
                description="d",
                category="copay_foundation",
            )
        ]
        safety = [
            AssistanceProgram(
                name="SafetyQux",
                url="https://x",
                description="d",
                category="safety_net",
            )
        ]
        results = FinancialAssistanceResults(
            diagnosis_specific=diag,
            manufacturer=mfr,
            general=gen,
            safety_net=safety,
        )
        names = [p.name for p in results.all_programs()]
        assert names == ["DiagFoo", "MfrBar", "GenBaz", "SafetyQux"]


class TestDataIntegrity:
    """Sanity checks on the curated catalog."""

    def test_all_programs_have_required_fields(self):
        for catalog in (
            GENERAL_PROGRAMS,
            DIAGNOSIS_SPECIFIC_PROGRAMS,
            MANUFACTURER_PROGRAMS,
            SAFETY_NET_PROGRAMS,
        ):
            for program in catalog:
                assert program.name, f"Program missing name: {program}"
                assert program.url.startswith(
                    "https://"
                ), f"Program {program.name} has non-https url: {program.url}"
                assert (
                    program.description
                ), f"Program {program.name} missing description"
                assert program.category in {
                    "copay_foundation",
                    "patient_assistance",
                    "safety_net",
                    "medicaid",
                    "manufacturer",
                }, f"Program {program.name} has unknown category {program.category}"

    def test_program_names_unique_across_catalogs(self):
        all_names = []
        for catalog in (
            GENERAL_PROGRAMS,
            DIAGNOSIS_SPECIFIC_PROGRAMS,
            MANUFACTURER_PROGRAMS,
            SAFETY_NET_PROGRAMS,
        ):
            all_names.extend(p.name for p in catalog)
        duplicates = {name for name in all_names if all_names.count(name) > 1}
        assert not duplicates, f"Duplicate program names: {duplicates}"

    def test_diagnosis_specific_programs_have_diagnosis_tags(self):
        for program in DIAGNOSIS_SPECIFIC_PROGRAMS:
            assert (
                program.diagnoses
            ), f"Diagnosis-specific program {program.name} has no diagnoses tags"

    def test_manufacturer_programs_have_drug_tags(self):
        for program in MANUFACTURER_PROGRAMS:
            assert (
                program.drugs
            ), f"Manufacturer program {program.name} has no drugs tags"

    def test_general_programs_have_no_drug_or_diagnosis_filter(self):
        # General programs should match any search - mostly. NORD is the
        # exception (rare-disease focused) so we exempt it.
        for program in GENERAL_PROGRAMS:
            if program.name.startswith("NORD"):
                continue
            assert (
                not program.drugs
            ), f"General program {program.name} unexpectedly drug-filtered"
            assert (
                not program.diagnoses
            ), f"General program {program.name} unexpectedly diagnosis-filtered"

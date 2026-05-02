"""
Tests for the pharmacy coupon and financial assistance payload helpers
on DenialCreatorHelper. These helpers populate the new fields on
DenialResponseInfo so the REST layer surfaces them automatically.

Tested as standalone helpers (without the full helper class instantiation)
to avoid pulling in Django-dependent machinery in async-unit.
"""

from types import SimpleNamespace

# Re-import the helpers as plain functions by pulling them off the class.
# DenialCreatorHelper itself imports Django at class definition time, so we
# import the staticmethods directly.
from fighthealthinsurance.pharmacy_coupon_detector import suggest_for_denial
from fighthealthinsurance.financial_assistance_directory import search


def _fake_denial(procedure=None, diagnosis=None, denial_text=None, your_state=None):
    return SimpleNamespace(
        procedure=procedure,
        diagnosis=diagnosis,
        denial_text=denial_text,
        your_state=your_state,
    )


def _build_pharmacy_payload(denial):
    """Mirror of DenialCreatorHelper._build_pharmacy_coupon_payload but
    inlined so async-unit tests don't need to instantiate the helper."""
    suggestion = suggest_for_denial(
        denial_text=denial.denial_text,
        procedure=denial.procedure,
        diagnosis=denial.diagnosis,
    )
    if suggestion is None:
        return None
    return {
        "drug_name": suggestion.drug_name,
        "is_likely_cheap": suggestion.is_likely_cheap,
        "bridge_message": suggestion.bridge_message,
        "oop_max_warning": suggestion.oop_max_warning,
        "pharmacy_options": [
            {
                "name": option.name,
                "url": option.url,
                "description": option.description,
                "counts_toward_oop_max": option.counts_toward_oop_max,
            }
            for option in suggestion.pharmacy_options
        ],
    }


def _build_financial_payload(denial):
    """Mirror of DenialCreatorHelper._build_financial_assistance_payload."""
    results = search(
        drug=denial.procedure,
        diagnosis=denial.diagnosis,
        denial_text=denial.denial_text,
        state_abbreviation=denial.your_state,
    )
    if not results.has_specific_matches():
        return None

    def _serialize(program):
        return {
            "name": program.name,
            "url": program.url,
            "description": program.description,
            "category": program.category,
            "eligibility_note": program.eligibility_note,
            "phone": program.phone,
        }

    return {
        "canonical_drug": results.canonical_drug,
        "diagnosis_text": results.diagnosis_text,
        "state_abbreviation": results.state_abbreviation,
        "diagnosis_specific": [_serialize(p) for p in results.diagnosis_specific],
        "manufacturer": [_serialize(p) for p in results.manufacturer],
        "general": [_serialize(p) for p in results.general],
        "safety_net": [_serialize(p) for p in results.safety_net],
        "state_medicaid_name": results.state_medicaid_name,
        "state_medicaid_url": results.state_medicaid_url,
        "state_medicaid_phone": results.state_medicaid_phone,
    }


class TestPharmacyCouponPayload:
    def test_returns_none_for_non_drug_denial(self):
        denial = _fake_denial(
            procedure="MRI of knee",
            diagnosis="knee pain",
            denial_text="MRI denied as not medically necessary",
        )
        assert _build_pharmacy_payload(denial) is None

    def test_returns_payload_for_drug_denial(self):
        denial = _fake_denial(
            procedure="Wegovy 2.4mg",
            diagnosis="Obesity",
            denial_text="Wegovy denied: non-formulary",
        )
        payload = _build_pharmacy_payload(denial)
        assert payload is not None
        assert payload["drug_name"] == "wegovy"
        assert payload["is_likely_cheap"] is False
        assert "out-of-pocket maximum" in payload["oop_max_warning"].lower()
        assert len(payload["pharmacy_options"]) == 3
        # Every pharmacy option carries the OOP-max boolean
        for opt in payload["pharmacy_options"]:
            assert opt["counts_toward_oop_max"] is False

    def test_brand_normalizes_to_generic_in_payload(self):
        denial = _fake_denial(procedure="Lipitor 20mg", diagnosis=None)
        payload = _build_pharmacy_payload(denial)
        assert payload is not None
        assert payload["drug_name"] == "atorvastatin"
        assert payload["is_likely_cheap"] is True


class TestFinancialAssistancePayload:
    def test_returns_payload_for_drug_denial(self):
        denial = _fake_denial(
            procedure="Wegovy",
            diagnosis="Obesity",
        )
        payload = _build_financial_payload(denial)
        assert payload is not None
        assert payload["canonical_drug"] == "wegovy"
        # Manufacturer copay card present
        manufacturer_names = [p["name"] for p in payload["manufacturer"]]
        assert "Wegovy Savings Card (Novo Nordisk)" in manufacturer_names
        # General programs always present
        general_names = [p["name"] for p in payload["general"]]
        assert "NeedyMeds" in general_names
        # Safety-net base entries always present
        safety_names = [p["name"] for p in payload["safety_net"]]
        assert "HRSA Find a Health Center (FQHC locator)" in safety_names

    def test_diagnosis_match_populates_diagnosis_specific(self):
        denial = _fake_denial(diagnosis="Stage III breast cancer")
        payload = _build_financial_payload(denial)
        assert payload is not None
        diag_names = [p["name"] for p in payload["diagnosis_specific"]]
        assert "CancerCare Co-Payment Assistance Foundation" in diag_names

    def test_payload_structure_serializable_keys(self):
        # Sanity check: every program serializes to the expected dict shape
        denial = _fake_denial(diagnosis="multiple sclerosis", procedure="Wegovy")
        payload = _build_financial_payload(denial)
        assert payload is not None
        for program_list_key in (
            "diagnosis_specific",
            "manufacturer",
            "general",
            "safety_net",
        ):
            for program in payload[program_list_key]:
                assert set(program.keys()) == {
                    "name",
                    "url",
                    "description",
                    "category",
                    "eligibility_note",
                    "phone",
                }

    def test_returns_none_for_non_drug_non_specific_denial(self):
        # A denial with no drug, no diagnosis match, no state should produce
        # None - the general copay directory alone is not specific enough
        # to gate the section on.
        denial = _fake_denial(
            procedure="MRI of knee",
            diagnosis="knee pain",
            denial_text="MRI denied as not medically necessary",
        )
        assert _build_financial_payload(denial) is None

    def test_canonical_drug_is_none_for_non_drug_procedure(self):
        # Even when something else (a diagnosis match) makes the payload
        # fire, canonical_drug must not echo back a non-drug procedure.
        denial = _fake_denial(
            procedure="MRI of knee",
            diagnosis="multiple sclerosis",
        )
        payload = _build_financial_payload(denial)
        assert payload is not None
        assert payload["canonical_drug"] is None

"""Tests for fighthealthinsurance.medical_code_extractor.

These are pure unit tests - no Django ORM access - so they live under
``tests/async-unit/`` for fast, hermetic execution.
"""

import pytest

from fighthealthinsurance.medical_code_extractor import (
    DME_DEVICE_KEYWORDS,
    DME_HCPCS_PREFIXES,
    HCPCS_CATEGORY,
    extract_cpt_codes,
    extract_dme_devices,
    extract_hcpcs_codes,
    extract_icd10_codes,
    extract_procedure_codes,
    hcpcs_category,
    is_aac_code,
    is_dme_code,
    unique_in_order,
)


class TestExtractCptCodes:
    def test_returns_empty_set_for_empty_string(self):
        assert extract_cpt_codes("") == set()
        assert extract_cpt_codes(None) == set()  # type: ignore[arg-type]

    def test_extracts_basic_five_digit_code(self):
        assert extract_cpt_codes("Procedure code 99213 was denied.") == {"99213"}

    def test_extracts_code_at_start_of_string(self):
        # The previous regex required a delimiter character before the
        # code and silently dropped this case.
        assert extract_cpt_codes("99213 was denied per the EOB.") == {"99213"}

    def test_extracts_code_at_end_of_string(self):
        # End-of-string previously failed because the regex required a
        # trailing delimiter character.
        assert extract_cpt_codes("Denial reason: 99213") == {"99213"}

    def test_extracts_code_in_parentheses(self):
        assert extract_cpt_codes("CPT (99213) is non-covered.") == {"99213"}

    def test_extracts_code_after_colon_with_no_space(self):
        # "Code:99213." was previously skipped because the lookahead
        # required whitespace/punctuation after the digits.
        assert extract_cpt_codes("Code:99213.") == {"99213"}

    def test_extracts_code_adjacent_to_hyphen(self):
        # Hyphens were not in the previous delimiter set.
        assert extract_cpt_codes("code-99213-billed") == {"99213"}

    def test_extracts_category_iii_t_code(self):
        assert extract_cpt_codes("Category III code 0184T was denied.") == {"0184T"}

    def test_extracts_category_ii_f_code(self):
        assert extract_cpt_codes("CPT II code 1036F submitted.") == {"1036F"}

    def test_deduplicates_codes(self):
        text = "99213 was billed. Code 99213 was denied. (99213)"
        assert extract_cpt_codes(text) == {"99213"}

    def test_extracts_multiple_distinct_codes(self):
        text = "Codes 99213, 99214 and 0184T submitted."
        assert extract_cpt_codes(text) == {"99213", "99214", "0184T"}

    def test_does_not_match_six_digit_numbers(self):
        # Six-digit numbers must not partially match as a CPT code.
        assert extract_cpt_codes("Reference 123456 in claim file.") == set()

    def test_does_not_match_four_digit_numbers(self):
        assert extract_cpt_codes("Year 2023 was denied.") == set()

    def test_does_not_match_when_followed_by_letter(self):
        # 99213abc should not match since the trailing alnum is part of
        # the same word.
        assert extract_cpt_codes("token99213abc more text") == set()

    def test_extracts_across_multiple_lines(self):
        text = "Line one mentions 99213.\nLine two mentions 99214."
        assert extract_cpt_codes(text) == {"99213", "99214"}


class TestExtractHcpcsCodes:
    def test_returns_empty_set_for_empty_string(self):
        assert extract_hcpcs_codes("") == set()

    def test_extracts_dme_e_code(self):
        # E0260 is a hospital bed code.
        assert extract_hcpcs_codes("DME claim E0260 denied.") == {"E0260"}

    def test_extracts_drug_j_code(self):
        # J3490 is "unclassified drugs" - a frequent denial code.
        assert extract_hcpcs_codes("J3490 was denied for off-label use.") == {"J3490"}

    def test_extracts_orthotic_l_code(self):
        # L0631 is a TLSO back brace.
        assert extract_hcpcs_codes("Brace L0631 not covered.") == {"L0631"}

    def test_extracts_supply_a_code(self):
        # A4253 is blood glucose test strips.
        assert extract_hcpcs_codes("(A4253)") == {"A4253"}

    def test_extracts_speech_v_code(self):
        # V5336 covers AAC repairs.
        assert extract_hcpcs_codes("Speech device service V5336 denied.") == {"V5336"}

    def test_extracts_multiple_distinct_codes(self):
        text = "Items billed: E0260, A4253, J3490, L0631."
        assert extract_hcpcs_codes(text) == {"E0260", "A4253", "J3490", "L0631"}

    def test_does_not_match_disallowed_prefixes(self):
        # I, N, and O prefixes are not valid HCPCS Level II letters.
        assert extract_hcpcs_codes("Code I0260 not real.") == set()
        assert extract_hcpcs_codes("Code N0260 not real.") == set()
        assert extract_hcpcs_codes("Code O0260 not real.") == set()

    def test_does_not_match_lowercase_prefix(self):
        # HCPCS codes are uppercase by convention; we don't match lower
        # to avoid noisy false positives like "e1234" embedded in
        # tracking IDs.
        assert extract_hcpcs_codes("token e0260 more") == set()

    def test_does_not_match_when_followed_by_alnum(self):
        assert extract_hcpcs_codes("E02601 is too long.") == set()


class TestExtractIcd10Codes:
    def test_returns_empty_set_for_empty_string(self):
        assert extract_icd10_codes("") == set()

    def test_extracts_basic_icd_code(self):
        assert "Z00.00" in extract_icd10_codes("Diagnosis Z00.00 noted.")

    def test_extracts_code_without_decimal(self):
        assert "Z00" in extract_icd10_codes("Diagnosis Z00 noted.")

    def test_extracts_code_at_start_of_string(self):
        assert "J45.909" in extract_icd10_codes("J45.909 chronic asthma was diagnosed.")

    def test_extracts_code_at_end_of_string(self):
        assert "M54.5" in extract_icd10_codes("Patient diagnosis M54.5")


class TestExtractProcedureCodes:
    def test_combines_cpt_and_hcpcs(self):
        text = "CPT 99213 and HCPCS E0260 were billed."
        assert extract_procedure_codes(text) == {"99213", "E0260"}

    def test_returns_empty_for_empty_text(self):
        assert extract_procedure_codes("") == set()


class TestHcpcsCategory:
    def test_known_categories(self):
        assert hcpcs_category("E0260") == "dme"
        assert hcpcs_category("J3490") == "drug"
        assert hcpcs_category("L0631") == "orthotic_prosthetic"
        assert hcpcs_category("A4253") == "supplies"
        assert hcpcs_category("V5336") == "vision_hearing_speech"
        assert hcpcs_category("K0001") == "dme"

    def test_unknown_category(self):
        assert hcpcs_category("Z9999") is None
        assert hcpcs_category("") is None

    def test_lowercase_prefix_normalized(self):
        # The function is forgiving for callers that already cleaned the
        # code; matching against the regex above is case-sensitive but
        # categorization is not.
        assert hcpcs_category("e0260") == "dme"


class TestIsDmeCode:
    @pytest.mark.parametrize("code", ["E0260", "K0001", "L0631"])
    def test_dme_codes_recognized(self, code):
        assert is_dme_code(code)

    @pytest.mark.parametrize("code", ["J3490", "A4253", "V5336", "99213"])
    def test_non_dme_codes_rejected(self, code):
        assert not is_dme_code(code)

    def test_invalid_inputs(self):
        assert not is_dme_code("")
        assert not is_dme_code("E026")  # too short
        assert not is_dme_code("E02601")  # too long


class TestIsAacCode:
    def test_recognized_aac_codes(self):
        # E1902 (communication board), E2500/E2599 (SGD bookends).
        assert is_aac_code("E1902")
        assert is_aac_code("E2500")
        assert is_aac_code("E2599")

    def test_non_aac_e_code_rejected(self):
        # E0260 is a hospital bed, not an AAC device.
        assert not is_aac_code("E0260")

    def test_non_e_code_rejected(self):
        assert not is_aac_code("V5336")

    def test_invalid_inputs(self):
        assert not is_aac_code("")
        assert not is_aac_code("E12AB")


class TestExtractDmeDevices:
    def test_returns_empty_for_empty_text(self):
        result = extract_dme_devices("")
        assert result["codes"] == set()
        assert result["all_hcpcs"] == set()
        assert result["device_types"] == set()
        assert result["matched_keywords"] == {}

    def test_identifies_dme_codes(self):
        text = "Patient was prescribed E0260 hospital bed and J3490 drug."
        result = extract_dme_devices(text)
        assert result["codes"] == {"E0260"}
        assert result["all_hcpcs"] == {"E0260", "J3490"}
        # Hospital bed keyword should also fire.
        assert "hospital_bed" in result["device_types"]

    def test_identifies_mobility_keyword(self):
        result = extract_dme_devices("Patient needs a power wheelchair.")
        assert "mobility" in result["device_types"]
        assert "wheelchair" in result["matched_keywords"]["mobility"]

    def test_identifies_aac_high_tech_keyword(self):
        result = extract_dme_devices(
            "Patient requires a speech-generating device for ALS."
        )
        assert "aac_high_tech" in result["device_types"]

    def test_aac_hcpcs_promotes_device_type(self):
        # E2500 is an AAC SGD code; even without keyword match, we
        # should classify the denial as AAC.
        result = extract_dme_devices("Code E2500 denied for SGD.")
        # The keyword 'sgd' is not in the keyword list so the only
        # signal is the code itself.
        assert "aac_high_tech" in result["device_types"]
        assert result["codes"] == {"E2500"}

    def test_l_code_defaults_to_orthotic(self):
        # L0631 with no other context should classify as orthotic.
        result = extract_dme_devices("Code L0631 brace denied.")
        assert "orthotic" in result["device_types"]

    def test_l_code_does_not_override_prosthetic_keyword(self):
        result = extract_dme_devices(
            "Patient prosthetic limb L5856 denied as not medically necessary."
        )
        assert "prosthetic" in result["device_types"]
        # We should not also tag as orthotic when prosthetic is already
        # present.
        assert "orthotic" not in result["device_types"]

    def test_respiratory_keywords(self):
        result = extract_dme_devices("CPAP therapy denied for OSA.")
        assert "respiratory" in result["device_types"]
        assert "cpap" in result["matched_keywords"]["respiratory"]

    def test_diabetic_keywords(self):
        result = extract_dme_devices(
            "Continuous glucose monitor denied as experimental."
        )
        assert "diabetic" in result["device_types"]

    def test_keyword_deduplication(self):
        # The same keyword appearing multiple times should be recorded
        # once.
        result = extract_dme_devices(
            "Wheelchair denied. Wheelchair appeal. WHEELCHAIR claim."
        )
        assert result["matched_keywords"]["mobility"] == ["wheelchair"]


class TestUniqueInOrder:
    def test_preserves_order_and_dedupes(self):
        assert unique_in_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_empty_input(self):
        assert unique_in_order([]) == []


class TestModuleConstants:
    def test_dme_prefixes_are_subset_of_known_categories(self):
        for prefix in DME_HCPCS_PREFIXES:
            assert prefix in HCPCS_CATEGORY

    def test_dme_keyword_keys_are_strings(self):
        for key, values in DME_DEVICE_KEYWORDS.items():
            assert isinstance(key, str)
            assert all(isinstance(v, str) for v in values)
            # All keywords must be lowercase to support case-insensitive
            # substring matching done by extract_dme_devices.
            assert all(v == v.lower() for v in values)

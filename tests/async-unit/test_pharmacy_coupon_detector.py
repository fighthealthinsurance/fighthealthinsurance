"""
Unit tests for the pharmacy coupon detector.
"""

from fighthealthinsurance.pharmacy_coupon_detector import (
    BRAND_TO_GENERIC,
    CHEAP_GENERIC_DRUGS,
    EXPENSIVE_DRUGS,
    PharmacyCouponSuggestion,
    build_suggestion,
    detect_drug,
    looks_like_prescription_denial,
    suggest_for_denial,
)


class TestDetectDrug:
    """Tests for the detect_drug function."""

    def test_detects_cheap_generic_in_text(self):
        assert detect_drug("Patient was prescribed metformin 500mg") == "metformin"

    def test_detects_cheap_generic_in_diagnosis_field(self):
        assert detect_drug(None, None, "Started on lisinopril for HTN") == "lisinopril"

    def test_normalizes_brand_to_generic(self):
        # Lipitor -> atorvastatin
        assert detect_drug("Refill request for Lipitor 20mg denied") == "atorvastatin"
        # Zoloft -> sertraline
        assert detect_drug("Patient on Zoloft") == "sertraline"

    def test_detects_expensive_drug(self):
        assert detect_drug("Wegovy denied for non-formulary") == "wegovy"

    def test_advair_detected_as_expensive_not_aliased_to_fluticasone(self):
        # Advair is a fluticasone+salmeterol combo; aliasing it to plain
        # fluticasone (a cheap generic) would falsely advertise a cash-pay
        # bridge that doesn't exist for the combo product.
        assert detect_drug("Advair denied for non-formulary") == "advair"

    def test_symbicort_detected_as_expensive(self):
        # Symbicort is a budesonide+formoterol combo - same reasoning.
        assert detect_drug("Symbicort denied") == "symbicort"

    def test_returns_none_when_no_drug_present(self):
        assert detect_drug("Knee MRI denied as not medically necessary") is None

    def test_returns_none_for_empty_inputs(self):
        assert detect_drug() is None
        assert detect_drug(None, None, "") is None

    def test_word_boundary_avoids_false_positive(self):
        # "metformin" should not match a substring inside another word
        assert detect_drug("submetformination is not a word") is None

    def test_case_insensitive(self):
        assert detect_drug("METFORMIN") == "metformin"
        assert detect_drug("MetFormin") == "metformin"

    def test_earliest_in_text_match_wins(self):
        # Detection is deterministic: the drug whose match starts earliest
        # in the text wins, regardless of dict/set iteration order.
        assert detect_drug("Patient on Lipitor and gabapentin") == "atorvastatin"
        assert detect_drug("Patient on gabapentin and Lipitor") == "gabapentin"
        # Brand-only text should produce the generic, not the brand.
        assert detect_drug("Lipitor") == "atorvastatin"

    def test_scans_multiple_texts_in_order(self):
        # First text with a hit wins
        assert detect_drug("nothing here", "metformin", "lisinopril") == "metformin"


class TestLooksLikePrescriptionDenial:
    """Tests for the looks_like_prescription_denial function."""

    def test_matches_formulary_cue(self):
        assert looks_like_prescription_denial("Drug not on formulary")

    def test_matches_step_therapy(self):
        assert looks_like_prescription_denial("Step therapy required")

    def test_matches_specialty_drug(self):
        assert looks_like_prescription_denial("This is a specialty drug")

    def test_no_match_for_unrelated_text(self):
        assert not looks_like_prescription_denial(
            "MRI denied for lack of medical necessity"
        )

    def test_handles_none_inputs(self):
        assert not looks_like_prescription_denial(None)
        assert not looks_like_prescription_denial(None, None, "")


class TestBuildSuggestion:
    """Tests for the build_suggestion function."""

    def test_cheap_generic_marked_as_likely_cheap(self):
        suggestion = build_suggestion("metformin")
        assert suggestion.is_likely_cheap is True
        assert "bridge" in suggestion.bridge_message.lower()

    def test_expensive_drug_marked_as_not_cheap(self):
        suggestion = build_suggestion("wegovy")
        assert suggestion.is_likely_cheap is False
        assert "expensive" in suggestion.bridge_message.lower()

    def test_includes_three_pharmacy_options(self):
        suggestion = build_suggestion("metformin")
        names = {opt.name for opt in suggestion.pharmacy_options}
        assert "GoodRx" in names
        assert "Mark Cuban Cost Plus Drugs" in names
        assert "Amazon Pharmacy" in names

    def test_pharmacy_urls_include_drug_name(self):
        suggestion = build_suggestion("metformin")
        for opt in suggestion.pharmacy_options:
            assert "metformin" in opt.url

    def test_amazon_url_uses_affiliate_tag(self):
        # The Amazon link must carry our Amazon Associates affiliate
        # parameters - dropping them would forfeit revenue that helps fund
        # the appeal-generation service.
        suggestion = build_suggestion("metformin")
        amazon = next(
            opt for opt in suggestion.pharmacy_options if opt.name == "Amazon Pharmacy"
        )
        assert amazon.url.startswith("https://www.amazon.com/s?k=metformin")
        assert "tag=totallylegitco-20" in amazon.url
        assert "linkCode=ll2" in amazon.url

    def test_oop_max_warning_present(self):
        suggestion = build_suggestion("metformin")
        # Discount-program payments do not count toward OOP max - this is the
        # critical caveat the user asked us to surface.
        assert "out-of-pocket maximum" in suggestion.oop_max_warning.lower()
        for opt in suggestion.pharmacy_options:
            assert opt.counts_toward_oop_max is False

    def test_url_encodes_special_characters(self):
        # Drug names with spaces or punctuation should be URL-encoded so the
        # generated links remain valid.
        suggestion = build_suggestion("estradiol valerate")
        for opt in suggestion.pharmacy_options:
            assert " " not in opt.url

    def test_drug_name_is_lowercased(self):
        suggestion = build_suggestion("Metformin")
        assert suggestion.drug_name == "metformin"


class TestSuggestForDenial:
    """Tests for the top-level suggest_for_denial entry point."""

    def test_returns_suggestion_for_drug_field(self):
        suggestion = suggest_for_denial(drug="metformin")
        assert suggestion is not None
        assert suggestion.drug_name == "metformin"
        assert suggestion.is_likely_cheap is True

    def test_drug_field_brand_normalizes_to_generic(self):
        suggestion = suggest_for_denial(drug="Lipitor")
        assert suggestion is not None
        assert suggestion.drug_name == "atorvastatin"

    def test_unknown_drug_field_still_returns_suggestion(self):
        # When the user explicitly tells us a drug name we don't recognize,
        # we still build a suggestion (just not marked as cheap).
        suggestion = suggest_for_denial(drug="some-unknown-drug")
        assert suggestion is not None
        assert suggestion.drug_name == "some-unknown-drug"
        assert suggestion.is_likely_cheap is False

    def test_detects_drug_in_denial_text(self):
        suggestion = suggest_for_denial(
            denial_text="Your request for sertraline 50mg is denied as non-formulary."
        )
        assert suggestion is not None
        assert suggestion.drug_name == "sertraline"
        assert suggestion.is_likely_cheap is True

    def test_detects_drug_in_procedure_field(self):
        suggestion = suggest_for_denial(procedure="lisinopril 10mg")
        assert suggestion is not None
        assert suggestion.drug_name == "lisinopril"

    def test_returns_generic_suggestion_for_prescription_cue_without_drug(self):
        suggestion = suggest_for_denial(
            denial_text="Your prescription drug is not on our formulary."
        )
        assert suggestion is not None
        # No specific drug detected, so drug_name is empty
        assert suggestion.drug_name == ""
        assert suggestion.is_likely_cheap is False
        # Should still provide pharmacy options pointing at search pages
        assert len(suggestion.pharmacy_options) == 3

    def test_returns_none_for_unrelated_denial(self):
        suggestion = suggest_for_denial(
            denial_text="Knee MRI denied - not medically necessary",
            procedure="MRI of knee",
            diagnosis="knee pain",
        )
        assert suggestion is None

    def test_returns_none_for_empty_inputs(self):
        assert suggest_for_denial() is None


class TestDataIntegrity:
    """Sanity checks on the static drug lists."""

    def test_brand_aliases_map_to_known_generics_or_drugs(self):
        # Every brand name must map to a generic the detector knows about.
        for brand, generic in BRAND_TO_GENERIC.items():
            assert (
                generic in CHEAP_GENERIC_DRUGS or generic in EXPENSIVE_DRUGS
            ), f"Brand '{brand}' maps to unknown generic '{generic}'"

    def test_cheap_and_expensive_lists_are_disjoint(self):
        overlap = CHEAP_GENERIC_DRUGS & EXPENSIVE_DRUGS
        assert not overlap, f"Drugs appear in both lists: {overlap}"

    def test_drug_names_are_lowercase(self):
        for drug in CHEAP_GENERIC_DRUGS | EXPENSIVE_DRUGS:
            assert drug == drug.lower()
        for brand in BRAND_TO_GENERIC:
            assert brand == brand.lower()

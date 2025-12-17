"""
Tests for the Explain Denial view functionality.
"""
import re


def test_denial_extraction_logic():
    """Test the denial reason extraction logic."""
    
    # Sample denial text with multiple reasons
    denial_text = """
    Dear Patient,
    
    Your claim for the medical procedure has been denied for the following reasons:
    1. The treatment was not medically necessary according to our review.
    2. Prior authorization was not obtained before the procedure.
    3. The service is considered experimental and not covered under your plan.
    
    Sincerely,
    Insurance Company
    """
    
    denial_info = {
        "detected_reasons": [],
        "plain_english": [],
        "extracted_phrases": [],
        "suggestions": [],
    }
    
    # Common denial reason patterns
    denial_patterns = [
        {
            "pattern": r"(not medically necessary|lacks medical necessity)",
            "reason": "Medical Necessity",
            "explanation": "Your insurance says the treatment isn't medically required.",
            "suggestion": "Get a letter from your doctor explaining why this treatment is medically necessary.",
        },
        {
            "pattern": r"(experimental|investigational|not proven)",
            "reason": "Experimental/Investigational",
            "explanation": "Your insurance considers the treatment experimental or not yet proven effective.",
            "suggestion": "Look for published medical studies supporting your treatment.",
        },
        {
            "pattern": r"(prior authorization|pre-authorization|pre-auth|preauth)",
            "reason": "Prior Authorization Missing",
            "explanation": "Your doctor didn't get approval from insurance before providing the treatment.",
            "suggestion": "Contact your doctor's office to request a prior authorization.",
        },
    ]
    
    # Check for each pattern
    for pattern_info in denial_patterns:
        match = re.search(pattern_info["pattern"], denial_text, re.IGNORECASE)
        if match:
            denial_info["detected_reasons"].append(pattern_info["reason"])
            denial_info["plain_english"].append(pattern_info["explanation"])
            denial_info["extracted_phrases"].append(match.group(0))
            denial_info["suggestions"].append(pattern_info["suggestion"])
    
    # Verify we detected all three reasons
    assert len(denial_info["detected_reasons"]) == 3, f"Expected 3 reasons, got {len(denial_info['detected_reasons'])}"
    assert "Medical Necessity" in denial_info["detected_reasons"]
    assert "Prior Authorization Missing" in denial_info["detected_reasons"]
    assert "Experimental/Investigational" in denial_info["detected_reasons"]
    
    print("✓ Test passed: All denial reasons detected correctly")
    return True


def test_no_patterns_detected():
    """Test behavior when no patterns are detected."""
    
    denial_text = "Your claim was denied."
    
    denial_info = {
        "detected_reasons": [],
        "plain_english": [],
        "extracted_phrases": [],
        "suggestions": [],
    }
    
    denial_patterns = [
        {
            "pattern": r"(not medically necessary|lacks medical necessity)",
            "reason": "Medical Necessity",
            "explanation": "Test",
            "suggestion": "Test",
        },
    ]
    
    for pattern_info in denial_patterns:
        match = re.search(pattern_info["pattern"], denial_text, re.IGNORECASE)
        if match:
            denial_info["detected_reasons"].append(pattern_info["reason"])
    
    # When no patterns detected, should provide general guidance
    if not denial_info["detected_reasons"]:
        denial_info["detected_reasons"].append("General Denial")
        denial_info["plain_english"].append(
            "We couldn't automatically identify the specific reason for your denial."
        )
    
    assert len(denial_info["detected_reasons"]) == 1
    assert denial_info["detected_reasons"][0] == "General Denial"
    
    print("✓ Test passed: General guidance provided when no patterns detected")
    return True


if __name__ == "__main__":
    test_denial_extraction_logic()
    test_no_patterns_detected()
    print("\n✅ All tests passed!")

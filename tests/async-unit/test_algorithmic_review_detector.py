from fighthealthinsurance.denials.algorithmic_review_detector import (
    detect_algorithmic_review_terms,
)


def test_automated_review_triggers_general_block():
    result = detect_algorithmic_review_terms("Your request was denied after automated review.")
    assert result.matched is True
    assert "algorithmic_review_general" in result.suggested_template_blocks


def test_interqual_triggers_criteria_request():
    result = detect_algorithmic_review_terms("Decision based on InterQual criteria.")
    assert result.matched is True
    assert "request_criteria_and_human_review" in result.suggested_template_blocks


def test_navihealth_or_nh_predict_triggers_vendor_block():
    result = detect_algorithmic_review_terms("Used nH Predict via naviHealth for post-acute review.")
    assert "vendor_specific_navihealth" in result.suggested_template_blocks


def test_footer_algorithm_does_not_overtrigger_high_confidence():
    text = "Claim denial for missing referral. Footer: algorithm v2 checksum id"
    result = detect_algorithmic_review_terms(text)
    assert result.matched is True
    assert result.confidence != "high"


def test_medicare_advantage_with_automation_includes_ma_block():
    text = "Medicare Advantage plan denial from automated review workflow."
    result = detect_algorithmic_review_terms(text)
    assert "algorithmic_review_medicare_advantage" in result.suggested_template_blocks


def test_ordinary_denial_language_no_false_positive():
    text = "Denied because prior authorization was not submitted and records were incomplete."
    result = detect_algorithmic_review_terms(text)
    assert result.matched is False
    assert result.suggested_template_blocks == []

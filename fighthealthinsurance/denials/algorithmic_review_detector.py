from dataclasses import dataclass
from typing import Literal
import re


@dataclass(frozen=True)
class AlgorithmicReviewDetectionResult:
    matched: bool
    confidence: Literal["low", "medium", "high"]
    matched_terms: list[str]
    vendor_matches: list[str]
    suggested_template_blocks: list[str]
    debug_reason: str


TERM_PATTERNS: dict[str, str] = {
    "automated review": r"\bautomated\s+review\b",
    "algorithmic determination": r"\balgorithmic\s+determination\b",
    "algorithm": r"\balgorithm(?:s|ic)?\b",
    "AI": r"\bai\b",
    "artificial intelligence": r"\bartificial\s+intelligence\b",
    "machine learning": r"\bmachine\s+learning\b",
    "clinical guideline": r"\bclinical\s+guideline(?:s)?\b",
    "proprietary criteria": r"\bproprietary\s+criteria\b",
    "medical necessity criteria": r"\bmedical\s+necessity\s+criteria\b",
    "utilization management": r"\butili[sz]ation\s+management\b",
}

VENDOR_PATTERNS: dict[str, str] = {
    "InterQual": r"\binterqual\b",
    "MCG": r"\bmcg\b(?:(?:\s*\(\s*care\s+guidelines?\s*\))|\s+(?:care\s+guidelines?|guideline(?:s)?|criteria|health))",
    "Milliman": r"\bmilliman\b",
    "NaviHealth": r"\bnavihealth\b|\bnh\s*predict\b",
    "EviCore": r"\bevicore\b",
    "Carelon": r"\bcarelon\b|\baim\s+specialty\s+health\b",
}


def _footer_only_algorithm_match(text: str, matched_terms: list[str]) -> bool:
    if "algorithm" not in matched_terms:
        return False
    lower = text.lower()
    if any(
        t in matched_terms
        for t in [
            "automated review",
            "algorithmic determination",
            "AI",
            "artificial intelligence",
            "machine learning",
        ]
    ):
        return False
    return bool(re.search(r"footer|template|document\s+id|checksum", lower))


TERM_WEIGHTS: dict[str, float] = {
    "automated review": 2.0,
    "algorithmic determination": 2.0,
    "algorithm": 0.5,
    "AI": 1.0,
    "artificial intelligence": 1.0,
    "machine learning": 1.5,
    "clinical guideline": 0.75,
    "proprietary criteria": 1.0,
    "medical necessity criteria": 1.0,
    "utilization management": 0.75,
}


def _dedupe_overlapping_terms(matched_terms: list[str]) -> list[str]:
    deduped = set(matched_terms)
    if "algorithmic determination" in deduped:
        deduped.discard("algorithm")
    if "artificial intelligence" in deduped:
        deduped.discard("AI")
    return sorted(deduped)


def detect_algorithmic_review_terms(text: str) -> AlgorithmicReviewDetectionResult:
    lower = (text or "").lower()
    matched_terms = [
        name for name, pat in TERM_PATTERNS.items() if re.search(pat, lower)
    ]
    matched_terms = _dedupe_overlapping_terms(matched_terms)
    vendor_matches = [
        name for name, pat in VENDOR_PATTERNS.items() if re.search(pat, lower)
    ]

    matched = bool(matched_terms or vendor_matches)
    score = sum(TERM_WEIGHTS.get(term, 1.0) for term in matched_terms) + (
        2.0 * len(vendor_matches)
    )
    if _footer_only_algorithm_match(lower, matched_terms):
        score = min(score, 1)

    if score >= 5:
        confidence: Literal["low", "medium", "high"] = "high"
    elif score >= 1.5:
        confidence = "medium"
    elif matched:
        confidence = "low"
    else:
        confidence = "low"

    blocks: list[str] = []
    if matched:
        blocks.extend(
            [
                "algorithmic_review_general",
                "request_criteria_and_human_review",
            ]
        )

    if (
        re.search(r"\bmedicare\s+advantage\b|\bpart\s*c\b|\bma\s+plan\b", lower)
        and matched
    ):
        blocks.append("algorithmic_review_medicare_advantage")

    if any(v == "NaviHealth" for v in vendor_matches):
        blocks.append("vendor_specific_navihealth")
    if any(v == "EviCore" for v in vendor_matches):
        blocks.append("vendor_specific_evicore")

    reason = (
        f"matched_terms={matched_terms}, vendor_matches={vendor_matches}, score={score}"
        if matched
        else "No algorithmic-review indicators detected"
    )

    return AlgorithmicReviewDetectionResult(
        matched=matched,
        confidence=confidence,
        matched_terms=matched_terms,
        vendor_matches=vendor_matches,
        suggested_template_blocks=blocks,
        debug_reason=reason,
    )


TEMPLATE_BLOCKS: dict[str, str] = {
    "algorithmic_review_general": (
        "I request disclosure of all criteria, guidelines, software tools, algorithms, vendor inputs, "
        "and decision rules used in this determination, and whether any automated or algorithmic process "
        "contributed to the denial."
    ),
    "algorithmic_review_medicare_advantage": (
        "If this is a Medicare Advantage coverage determination, please confirm that any prior authorization "
        "or decision-support tools were used only to assess diagnosis/medical criteria and medical necessity, "
        "and did not override Medicare coverage rules or individualized clinical facts, consistent with CMS's "
        "2024 Medicare Advantage final rule and CMS FAQ guidance on algorithm-assisted determinations."
    ),
    "vendor_specific_navihealth": (
        "If any naviHealth/nH Predict or related post-acute decision-support tool was consulted, "
        "please identify exactly how it was used in this case, the inputs relied on, and how the treating "
        "clinician's findings were weighed. I note there has been reported litigation with allegations about "
        "such tools; this request is to preserve my appeal rights and clarify the record for this individual case."
    ),
    "vendor_specific_evicore": (
        "If EviCore was involved in utilization management or medical-necessity review for this decision, "
        "please provide the specific criteria set, version, and rationale applied to my documented condition."
    ),
    "request_criteria_and_human_review": (
        "Please provide copies of the medical-necessity criteria and plan provisions relied upon, explain how "
        "those criteria apply to my specific documented condition, and ensure this appeal is reviewed by an "
        "appropriately qualified clinician based on individualized clinical facts."
    ),
}


def render_template_blocks(block_ids: list[str]) -> list[str]:
    return [
        TEMPLATE_BLOCKS[block_id]
        for block_id in block_ids
        if block_id in TEMPLATE_BLOCKS
    ]

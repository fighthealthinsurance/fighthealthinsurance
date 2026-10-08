"""The v1 and v2 appeal prompts, pinned byte for byte.

v1 is the original appeal prompt and v2 is v1 with the output contract
appended. Adding another version must not change a single byte of either, or
the drafts already stamped v1/v2 stop being comparable with new ones. The
strings below were captured from the code before v3 existed, with the
professional-voice example shuffle seeded, so any later change to what v1 or
v2 sends fails here.
"""

import itertools
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
)
from fighthealthinsurance.ml import appeal_prompt_versions as apv
from fighthealthinsurance.ml.ml_models import RemoteModelLike

# make_open_prompt shuffles its professional-voice example openings with the
# module-level random; each build here gets its own generator with this seed.
SEED = 1086

PROMPT_SCENARIOS = {
    "patient_minimal": dict(
        denial_text=(
            "Your request for a lumbar spine MRI is denied as not medically "
            "necessary."
        ),
        procedure="Lumbar MRI",
        diagnosis="Low back pain",
    ),
    "professional_with_details": dict(
        denial_text=(
            "Coverage for physical therapy beyond 20 visits is denied. You may "
            "appeal within 180 days."
        ),
        procedure="Physical therapy",
        is_trans=True,
        patient="Alex Kim",
        professional="Dr. Jane Rivera, MD",
        qa_context="Symptoms have lasted six months despite home exercise.",
        professional_to_finish=True,
        plan_id="PLN-778",
        claim_id="CLM-1234",
        insurance_company="Acme Health",
        is_tpa=True,
        plan_context=(
            "Gold PPO; therapy visits capped at 20 per year unless medically "
            "necessary."
        ),
    ),
    "every_context_block": dict(
        denial_text="The requested infusion is experimental and investigational.",
        procedure="Infliximab infusion",
        diagnosis="Crohn's disease",
        professional_to_finish=True,
        insurance_company="Acme Health",
        claim_id="Acme Health",
        plan_id="UNKNOWN",
        ml_context="[1] Example citation one\n[2] Example citation two",
        pubmed_context="PMID 1: Example abstract.",
        rag_context="Guideline: infusions are standard of care.",
        nice_context="NICE TA187 recommends infliximab.",
        uspstf_context="USPSTF block text.",
        clinical_trials_context="Clinical trials block text.",
        ucr_context="UCR block text.",
        payer_policy_context="Other insurers cover this service.",
        pa_context="Rule 1: infusions need prior authorization.",
        medication_context="TNF inhibitor guidance.",
        regulatory_citation_context="State prior-authorization reform block.",
        plan_law_context="ERISA governs this plan.",
    ),
    "unknown_and_blank_values": dict(
        denial_text="Denied.",
        procedure="",
        diagnosis="Asthma",
        qa_context="UNKNOWN",
        plan_id="",
        claim_id="UNKNOWN",
        insurance_company="UNKNOWN",
        pa_context="   ",
        plan_context="short",
        ml_context="  ",
    ),
}


def _seeded_open_prompt(**kwargs):
    with patch("fighthealthinsurance.generate_appeal.random", random.Random(SEED)):
        return AppealGenerator().make_open_prompt(**kwargs)


class _RecordingBackend(RemoteModelLike):
    """Writes a usable letter and remembers every prompt it was sent."""

    def __init__(self):
        self.prompts = []

    async def _infer(self, *args, **kwargs):
        raise AssertionError("make_appeals should use the sync infer seam")

    def infer(
        self,
        prompt,
        patient_context,
        plan_context,
        infer_type,
        pubmed_context,
        ml_citations_context,
        prof_pov=False,
    ):
        self.prompts.append((infer_type, prompt))
        return [
            (
                infer_type,
                "Dear Appeals Department,\n\n"
                + "I am writing to appeal the denial of the lumbar MRI. " * 12
                + "\n\nSincerely,\nDr. Jane Rivera, MD",
            )
        ]


def _make_appeals_prompts(prompt_mode):
    """Every prompt make_appeals sends, per model, for one fixed denial with
    the specialized-hint call and the enrichment blocks switched on."""
    denial = SimpleNamespace(
        denial_text=(
            "Coverage for the requested lumbar MRI is denied as not medically "
            "necessary."
        ),
        procedure="Lumbar MRI",
        diagnosis="Low back pain",
        patient_user=None,
        primary_professional="Dr. Jane Rivera, MD",
        qa_context='{"How long has the pain lasted?": "Six months"}',
        health_history=None,
        professional_to_finish=True,
        plan_id="PLN-778",
        claim_id="CLM-1234",
        insurance_company="Acme Health",
        insurance_company_obj=None,
        plan_context=None,
        plan_documents_summary=None,
        use_external=False,
        denial_id=7,
        ucr_context={"narrative": "UCR narrative for lumbar MRI."},
    )
    fakes = {name: _RecordingBackend() for name in ("model-a", "model-b")}
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=lambda use_external=False: list(fakes),
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new={name: [fake] for name, fake in fakes.items()},
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ), patch(
        "fighthealthinsurance.generate_appeal.random", random.Random(SEED)
    ), patch.object(
        AppealGenerator, "_best_internal_model_name", return_value="model-a"
    ), patch.object(
        AppealGenerator,
        "_build_specialized_hint_block",
        return_value="Denial-type hint: cite the parity law.",
    ), patch.object(
        AppealGenerator,
        "_collect_medication_context",
        return_value="Medication guidance block.",
    ), patch.object(
        AppealGenerator,
        "_collect_regulatory_context",
        return_value="Regulatory citation block.",
    ), patch.object(
        AppealGenerator,
        "_collect_plan_law_context",
        return_value="Plan law block: ERISA.",
    ):
        list(
            AppealGenerator().make_appeals(
                denial,
                AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"]),
                medical_reasons=[],
                non_ai_appeals=[],
                pubmed_context="PMID 2: Example abstract.",
                ml_citations_context=["[1] Citation A", "[2] Citation B"],
                plan_context="Gold PPO plan; imaging needs prior authorization.",
                payer_policy_context="Other insurers cover lumbar MRI.",
                specialized_templates=[object()],
                prompt_mode=prompt_mode,
            )
        )
    return {
        name: sorted(p for t, p in fake.prompts if t == "full")
        for name, fake in fakes.items()
    }


GOLDEN_V1 = {
    "patient_minimal": (
        "\n"
        "\n"
        "IMPORTANT: No specific medical citations have been provided. Do "
        "NOT invent or hallucinate any citations, PMIDs, NCT IDs, journal "
        "names, or study references. You may state general medical "
        "knowledge without citations, but do not fabricate specific study "
        "references.Write a health insurance appeal for procedure Lumbar "
        "MRI with diagnosis Low back pain given the following denial:\n"
        "Your request for a lumbar spine MRI is denied as not medically "
        "necessary."
    ),
    "professional_with_details": (
        "While answering the question keep in mind the patient is trans. "
        "The patient's insurance plan details are as follows: Gold PPO; "
        "therapy visits capped at 20 per year unless medically necessary..\n"
        "IMPORTANT: Please write the appeal as the healthcare professional "
        "(not the patient), using 'I' for yourself and referring to the "
        "patient in the third person (e.g., 'the patient', 'they'). Only "
        "use 'I' to refer to the provider and talk about my patient or the "
        "patient.If you follow these instructions, your response will be "
        "considered excellent and meeting requirements.\n"
        "Good phrases and approaches that lead to winning appeals:\n"
        "was recommended for the patient\n"
        "The patient has been experiencing\n"
        "the patient's pain\n"
        "the patient's health\n"
        "the patient's condition\n"
        "[patient's name]\n"
        "the patient is experiencing\n"
        "Any language that makes it clear the letter is written by the "
        "doctor or healthcare professional about the patient.\n"
        "\n"
        "Write from your perspective as the healthcare professional, using "
        "'I' for yourself and referring to the patient in the third person "
        "(e.g., 'the patient,' 'they').\n"
        "Forbidden any language that implies the letter is written by the "
        "patient.\n"
        "GOOD EXAMPLE: I am writing to appeal the denial of coverage for "
        "[insert procedure] for my patient, [insert patient's name].\n"
        "GOOD EXAMPLE: As the medical professional overseeing this "
        "patient’s care, I am appealing the denial of coverage.\n"
        "GOOD EXAMPLE: I am submitting this appeal on behalf of my patient "
        "in support of coverage for the recommended treatment, based on my "
        "clinical assessment and the patient’s ongoing medical needs.\n"
        "GOOD EXAMPLE: As the treating physician, I am writing to appeal "
        "the denial of coverage for my patient.\n"
        "Sign the letter as Dr. Jane Rivera, MD.\n"
        "Thank you for following these instructions.\n"
        ". You should try and incorporate the following QA context into "
        "your appeal: Symptoms have lasted six months despite home "
        "exercise... Please include and fill in the patients info Alex "
        "Kim.. Please include and fill in the professionals info Dr. Jane "
        "Rivera, MD.. Please include and fill in any references to the "
        "plan id as PLN-778.\n"
        "\n"
        "IMPORTANT: No specific medical citations have been provided. Do "
        "NOT invent or hallucinate any citations, PMIDs, NCT IDs, journal "
        "names, or study references. You may state general medical "
        "knowledge without citations, but do not fabricate specific study "
        "references.. Please include and fill in any references to the "
        "insurance company to be Acme Health. Note: This insurance company "
        "is a Third-Party Administrator (TPA) for self-funded employer "
        "plans, which are typically governed by ERISA (Employee Retirement "
        "Income Security Act). ERISA plans have specific appeal "
        "requirements and timelines. The employer is the plan fiduciary "
        "and ultimately responsible for coverage decisions, though the TPA "
        "administers claims.. Please include and fill in any references to "
        "the claim id as CLM-1234.Write a health insurance appeal for "
        "procedure Physical therapy given the following denial:\n"
        "Coverage for physical therapy beyond 20 visits is denied. You may "
        "appeal within 180 days."
    ),
    "every_context_block": (
        "\n"
        "IMPORTANT: Please write the appeal as the healthcare professional "
        "(not the patient), using 'I' for yourself and referring to the "
        "patient in the third person (e.g., 'the patient', 'they'). Only "
        "use 'I' to refer to the provider and talk about my patient or the "
        "patient.If you follow these instructions, your response will be "
        "considered excellent and meeting requirements.\n"
        "Good phrases and approaches that lead to winning appeals:\n"
        "was recommended for the patient\n"
        "The patient has been experiencing\n"
        "the patient's pain\n"
        "the patient's health\n"
        "the patient's condition\n"
        "[patient's name]\n"
        "the patient is experiencing\n"
        "Any language that makes it clear the letter is written by the "
        "doctor or healthcare professional about the patient.\n"
        "\n"
        "Write from your perspective as the healthcare professional, using "
        "'I' for yourself and referring to the patient in the third person "
        "(e.g., 'the patient,' 'they').\n"
        "Forbidden any language that implies the letter is written by the "
        "patient.\n"
        "GOOD EXAMPLE: I am writing to appeal the denial of coverage for "
        "[insert procedure] for my patient, [insert patient's name].\n"
        "GOOD EXAMPLE: As the medical professional overseeing this "
        "patient’s care, I am appealing the denial of coverage.\n"
        "GOOD EXAMPLE: I am submitting this appeal on behalf of my patient "
        "in support of coverage for the recommended treatment, based on my "
        "clinical assessment and the patient’s ongoing medical needs.\n"
        "GOOD EXAMPLE: As the treating physician, I am writing to appeal "
        "the denial of coverage for my patient.\n"
        "Thank you for following these instructions.\n"
        "\n"
        "\n"
        "PAYER PRIOR-AUTH RULES: The following entries come from the "
        "payer's own published prior-authorization requirement list. Use "
        "them when the denial relies on PA grounds — point out exactly "
        "which rule (or absence of one) supports approval, cite the "
        "criteria document by name, and reference the published submission "
        "channel where relevant. Do not invent rules that are not listed "
        "below.\n"
        "Rule 1: infusions need prior authorization.\n"
        "\n"
        "CITATION INSTRUCTIONS: You may ONLY cite medical literature, "
        "studies, or references that are explicitly provided below. Do NOT "
        "invent, fabricate, or hallucinate any citations, PMIDs, NCT IDs, "
        "journal names, author names, or study details. If you want to "
        "make a medical claim, either cite from the provided references or "
        "state it as general medical knowledge without a specific "
        "citation.\n"
        "\n"
        "Evidence from medical guidelines and regulations:\n"
        "Guideline: infusions are standard of care.\n"
        "\n"
        "Provided citations (use these): [1] Example citation one\n"
        "[2] Example citation two\n"
        "\n"
        "PubMed references (use these): PMID 1: Example abstract.\n"
        "\n"
        "NICE (UK) guidance:\n"
        "NICE TA187 recommends infliximab.\n"
        "\n"
        "USPSTF block text.\n"
        "\n"
        "Clinical trials block text.\n"
        "\n"
        "UCR PRICING CONTEXT: The denial may involve out-of-network "
        "under-reimbursement. The [UCR PRICING CONTEXT] block below "
        "carries an independent rate benchmark for this procedure and "
        "geographic area. If it strengthens the appeal — e.g. arguing the "
        "plan's allowable methodology is below typical rates — cite the "
        "source and effective date verbatim. If it isn't relevant to the "
        "arguments you're making, you may omit it. Do NOT invent rates or "
        "percentile values that are not in the block.\n"
        "\n"
        "UCR block text.\n"
        "\n"
        "DRUG-CLASS GUIDANCE: The medication(s) involved fall into a class "
        "with known appeal strategies. Use the following curated context "
        "where relevant. Do not invent citations beyond those listed "
        "elsewhere.\n"
        "TNF inhibitor guidance.\n"
        "\n"
        "State prior-authorization reform block.\n"
        "\n"
        "ERISA governs this plan.. Please include and fill in any "
        "references to the insurance company to be Acme Health.\n"
        "\n"
        "Other insurers cover this service.\n"
        "\n"
        "When using the comparative payer-policy information above, frame "
        "it as supporting industry context (other major insurers recognize "
        "this service as medically necessary under documented criteria), "
        "and do NOT assert that another payer's policy binds the patient's "
        "plan. Always defer to the patient's own plan documents for what "
        "is actually covered.Write a health insurance appeal for procedure "
        "Infliximab infusion with diagnosis Crohn's disease given the "
        "following denial:\n"
        "The requested infusion is experimental and investigational."
    ),
    "unknown_and_blank_values": (
        "\n"
        "\n"
        "CITATION INSTRUCTIONS: You may ONLY cite medical literature, "
        "studies, or references that are explicitly provided below. Do NOT "
        "invent, fabricate, or hallucinate any citations, PMIDs, NCT IDs, "
        "journal names, author names, or study details. If you want to "
        "make a medical claim, either cite from the provided references or "
        "state it as general medical knowledge without a specific "
        "citation.\n"
        "\n"
        "Provided citations (use these):   Write a health insurance appeal "
        "for the following denial:\n"
        "Denied."
    ),
}

# What make_appeals sends every full-letter call for the denial in
# _make_appeals_prompts, before any hint block or contract.
GOLDEN_MAKE_APPEALS_V1 = (
    " The patient's insurance plan details are as follows: Gold PPO "
    "plan; imaging needs prior authorization..\n"
    "IMPORTANT: Please write the appeal as the healthcare professional "
    "(not the patient), using 'I' for yourself and referring to the "
    "patient in the third person (e.g., 'the patient', 'they'). Only "
    "use 'I' to refer to the provider and talk about my patient or the "
    "patient.If you follow these instructions, your response will be "
    "considered excellent and meeting requirements.\n"
    "Good phrases and approaches that lead to winning appeals:\n"
    "was recommended for the patient\n"
    "The patient has been experiencing\n"
    "the patient's pain\n"
    "the patient's health\n"
    "the patient's condition\n"
    "[patient's name]\n"
    "the patient is experiencing\n"
    "Any language that makes it clear the letter is written by the "
    "doctor or healthcare professional about the patient.\n"
    "\n"
    "Write from your perspective as the healthcare professional, using "
    "'I' for yourself and referring to the patient in the third person "
    "(e.g., 'the patient,' 'they').\n"
    "Forbidden any language that implies the letter is written by the "
    "patient.\n"
    "GOOD EXAMPLE: I am writing to appeal the denial of coverage for "
    "[insert procedure] for my patient, [insert patient's name].\n"
    "GOOD EXAMPLE: As the medical professional overseeing this "
    "patient’s care, I am appealing the denial of coverage.\n"
    "GOOD EXAMPLE: I am submitting this appeal on behalf of my patient "
    "in support of coverage for the recommended treatment, based on my "
    "clinical assessment and the patient’s ongoing medical needs.\n"
    "GOOD EXAMPLE: As the treating physician, I am writing to appeal "
    "the denial of coverage for my patient.\n"
    "Sign the letter as Dr. Jane Rivera, MD.\n"
    "Thank you for following these instructions.\n"
    ". You should try and incorporate the following QA context into "
    'your appeal: {"How long has the pain lasted?": "Six months"}.. '
    "Please include and fill in the professionals info Dr. Jane "
    "Rivera, MD.. Please include and fill in any references to the "
    "plan id as PLN-778.\n"
    "\n"
    "CITATION INSTRUCTIONS: You may ONLY cite medical literature, "
    "studies, or references that are explicitly provided below. Do NOT "
    "invent, fabricate, or hallucinate any citations, PMIDs, NCT IDs, "
    "journal names, author names, or study details. If you want to "
    "make a medical claim, either cite from the provided references or "
    "state it as general medical knowledge without a specific "
    "citation.\n"
    "\n"
    "Provided citations (use these): [1] Citation A\n"
    "[2] Citation B\n"
    "\n"
    "PubMed references (use these): PMID 2: Example abstract.\n"
    "\n"
    "UCR PRICING CONTEXT: The denial may involve out-of-network "
    "under-reimbursement. The [UCR PRICING CONTEXT] block below "
    "carries an independent rate benchmark for this procedure and "
    "geographic area. If it strengthens the appeal — e.g. arguing the "
    "plan's allowable methodology is below typical rates — cite the "
    "source and effective date verbatim. If it isn't relevant to the "
    "arguments you're making, you may omit it. Do NOT invent rates or "
    "percentile values that are not in the block.\n"
    "\n"
    "UCR narrative for lumbar MRI.\n"
    "\n"
    "DRUG-CLASS GUIDANCE: The medication(s) involved fall into a class "
    "with known appeal strategies. Use the following curated context "
    "where relevant. Do not invent citations beyond those listed "
    "elsewhere.\n"
    "Medication guidance block.\n"
    "\n"
    "Regulatory citation block.\n"
    "\n"
    "Plan law block: ERISA.. Please include and fill in any references "
    "to the insurance company to be Acme Health.\n"
    "\n"
    "Other insurers cover lumbar MRI.\n"
    "\n"
    "When using the comparative payer-policy information above, frame "
    "it as supporting industry context (other major insurers recognize "
    "this service as medically necessary under documented criteria), "
    "and do NOT assert that another payer's policy binds the patient's "
    "plan. Always defer to the patient's own plan documents for what "
    "is actually covered.. Please include and fill in any references "
    "to the claim id as CLM-1234.Write a health insurance appeal for "
    "procedure Lumbar MRI with diagnosis Low back pain given the "
    "following denial:\n"
    "Coverage for the requested lumbar MRI is denied as not medically "
    "necessary."
)

# What v2 adds after the whole v1 prompt.
V2_TAIL = (
    "\n"
    "\n"
    "Output ONLY the letter itself: no markdown formatting or "
    "headings, no commentary before or after, no notes to the user. "
    "Plain prose, and end immediately after the signature block."
)

# The specialized-hint call adds this to the open prompt, before any contract.
SPECIALIZED_TAIL = (
    "\n\n--- Denial-type guidance ---\nDenial-type hint: cite the parity law."
)


@pytest.mark.parametrize("name", sorted(PROMPT_SCENARIOS))
def test_the_v1_open_prompt_is_unchanged(name):
    assert _seeded_open_prompt(**PROMPT_SCENARIOS[name]) == GOLDEN_V1[name]


@pytest.mark.parametrize("name", sorted(PROMPT_SCENARIOS))
def test_the_v2_prompt_is_the_unchanged_v1_prompt_plus_the_contract(name):
    prompt = apv.apply_prompt_version(
        _seeded_open_prompt(**PROMPT_SCENARIOS[name]), apv.PROMPT_V2
    )
    assert prompt == GOLDEN_V1[name] + V2_TAIL


def test_make_appeals_sends_the_unchanged_v1_prompts():
    assert _make_appeals_prompts(apv.MODE_ORIGINAL) == {
        "model-a": sorted(
            [GOLDEN_MAKE_APPEALS_V1, GOLDEN_MAKE_APPEALS_V1 + SPECIALIZED_TAIL]
        ),
        "model-b": [GOLDEN_MAKE_APPEALS_V1],
    }


def test_make_appeals_sends_the_unchanged_v2_prompts():
    assert _make_appeals_prompts(apv.MODE_NEW) == {
        "model-a": sorted(
            [
                GOLDEN_MAKE_APPEALS_V1 + V2_TAIL,
                GOLDEN_MAKE_APPEALS_V1 + SPECIALIZED_TAIL + V2_TAIL,
            ]
        ),
        "model-b": [GOLDEN_MAKE_APPEALS_V1 + V2_TAIL],
    }


@pytest.mark.parametrize("draw,tail", [(0.1, ""), (0.5, V2_TAIL)], ids=["v1", "v2"])
def test_thirds_sends_the_unchanged_v1_and_v2_prompts_beside_v3(draw, tail):
    # model-a draws v3 and model-b the version under test: building the
    # sectioned prompt in the same run leaves the other calls' bytes alone.
    draws = itertools.cycle([0.9, draw])
    with patch.object(apv, "_split_draw", lambda: next(draws)):
        prompts = _make_appeals_prompts(apv.MODE_THIRDS)
    assert prompts["model-b"] == [GOLDEN_MAKE_APPEALS_V1 + tail]
    assert prompts["model-a"] and all(
        p.startswith("TASK: ") for p in prompts["model-a"]
    )

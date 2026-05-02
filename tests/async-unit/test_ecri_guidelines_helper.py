"""
Unit tests for the ECRI Guidelines Trust integration.
"""

from datetime import date

import pytest

from fighthealthinsurance.ecri_guidelines_helper import ECRIGuidelinesHelper
from fighthealthinsurance.models import ECRIGuideline


async def _create_cardiac_guideline() -> ECRIGuideline:
    return await ECRIGuideline.objects.acreate(
        guideline_id="test-acc-aha-cad",
        title="Chronic Coronary Disease Management",
        developer_organization="ACC/AHA",
        publication_date=date(2023, 7, 20),
        recommendations_summary="Use guideline-directed medical therapy.",
        procedure_keywords=["pci", "coronary angiography", "cabg"],
        diagnosis_keywords=["coronary artery disease", "stable angina"],
        topics=["cardiology"],
        url="https://guidelines.ecri.org/example-cad",
    )


async def _create_diabetes_guideline() -> ECRIGuideline:
    return await ECRIGuideline.objects.acreate(
        guideline_id="test-ada-diabetes",
        title="Standards of Care in Diabetes",
        developer_organization="American Diabetes Association",
        publication_date=date(2024, 1, 1),
        recommendations_summary="Prefer GLP-1 RA or SGLT2i with CVD/CKD/HF.",
        procedure_keywords=["semaglutide", "glp-1 agonist", "cgm"],
        diagnosis_keywords=["type 2 diabetes", "diabetes mellitus"],
        topics=["endocrinology"],
        url="https://guidelines.ecri.org/example-diabetes",
    )


async def _create_inactive_guideline() -> ECRIGuideline:
    return await ECRIGuideline.objects.acreate(
        guideline_id="test-inactive",
        title="Old Cardiac Guideline",
        procedure_keywords=["pci"],
        diagnosis_keywords=["coronary artery disease"],
        is_active=False,
    )


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_citation_string_includes_org_year_and_url():
    guideline = await _create_cardiac_guideline()
    citation = guideline.citation_string()
    assert "Chronic Coronary Disease Management" in citation
    assert "ACC/AHA" in citation
    assert "2023" in citation
    assert "https://guidelines.ecri.org/example-cad" in citation
    assert "ECRI Guidelines Trust" in citation


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_find_relevant_guidelines_returns_empty_with_no_inputs():
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="", diagnosis=""
    )
    assert results == []


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_find_relevant_guidelines_matches_on_procedure_keyword():
    await _create_cardiac_guideline()
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="Coronary Angiography",
        diagnosis="",
    )
    assert len(results) == 1
    assert results[0].guideline_id == "test-acc-aha-cad"


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_find_relevant_guidelines_matches_on_diagnosis_keyword():
    await _create_cardiac_guideline()
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="",
        diagnosis="stable angina",
    )
    assert len(results) == 1


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_find_relevant_guidelines_excludes_inactive():
    await _create_cardiac_guideline()
    await _create_inactive_guideline()
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="pci",
        diagnosis="coronary artery disease",
    )
    ids = {g.guideline_id for g in results}
    assert "test-acc-aha-cad" in ids
    assert "test-inactive" not in ids


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_find_relevant_guidelines_max_results_respected():
    await _create_cardiac_guideline()
    await _create_diabetes_guideline()
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="pci cgm",
        diagnosis="",
        max_results=1,
    )
    assert len(results) == 1


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_find_relevant_guidelines_no_match_returns_empty():
    await _create_cardiac_guideline()
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="hip replacement",
        diagnosis="hip osteoarthritis",
    )
    assert results == []


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_citations_formats_each_match():
    await _create_cardiac_guideline()
    citations = await ECRIGuidelinesHelper.get_citations(
        procedure="pci",
        diagnosis="coronary artery disease",
    )
    assert len(citations) == 1
    assert "Chronic Coronary Disease Management" in citations[0]
    assert "ECRI Guidelines Trust" in citations[0]


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_context_includes_summary_and_url():
    await _create_cardiac_guideline()
    context = await ECRIGuidelinesHelper.get_context(
        procedure="pci",
        diagnosis="coronary artery disease",
    )
    assert "Evidence-Based Clinical Practice Guidelines" in context
    assert "Chronic Coronary Disease Management" in context
    assert "guideline-directed medical therapy" in context
    assert "https://guidelines.ecri.org/example-cad" in context


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_context_truncates_long_summary():
    await ECRIGuideline.objects.acreate(
        guideline_id="test-long",
        title="Big Guideline",
        recommendations_summary="X" * 5000,
        procedure_keywords=["bigproc"],
        diagnosis_keywords=["bigdx"],
    )
    context = await ECRIGuidelinesHelper.get_context(
        procedure="bigproc",
        diagnosis="bigdx",
        max_chars_per_guideline=200,
    )
    assert "..." in context
    assert "X" * 250 not in context


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_get_context_empty_when_no_match():
    context = await ECRIGuidelinesHelper.get_context(
        procedure="nonexistent procedure",
        diagnosis="nonexistent diagnosis",
    )
    assert context == ""

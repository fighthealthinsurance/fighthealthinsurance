"""
Unit tests for the ECRI Guidelines Trust integration.
"""

from datetime import date

import pytest

from fighthealthinsurance.ecri_guidelines_helper import ECRIGuidelinesHelper
from fighthealthinsurance.models import ECRIGuideline

# Each test uses a unique guideline_id so parallel test workers don't collide
# on the unique constraint when the django_db transaction rollback races with
# the async write.


async def _create_cardiac_guideline(suffix: str) -> ECRIGuideline:
    return await ECRIGuideline.objects.acreate(
        guideline_id=f"test-acc-aha-cad-{suffix}",
        title="Chronic Coronary Disease Management",
        developer_organization="ACC/AHA",
        publication_date=date(2023, 7, 20),
        recommendations_summary="Use guideline-directed medical therapy.",
        procedure_keywords=["pci", "coronary angiography", "cabg"],
        diagnosis_keywords=["coronary artery disease", "stable angina"],
        topics=["cardiology"],
        url="https://guidelines.ecri.org/example-cad",
    )


async def _create_diabetes_guideline(suffix: str) -> ECRIGuideline:
    return await ECRIGuideline.objects.acreate(
        guideline_id=f"test-ada-diabetes-{suffix}",
        title="Standards of Care in Diabetes",
        developer_organization="American Diabetes Association",
        publication_date=date(2024, 1, 1),
        recommendations_summary="Prefer GLP-1 RA or SGLT2i with CVD/CKD/HF.",
        procedure_keywords=["semaglutide", "glp-1 agonist", "cgm"],
        diagnosis_keywords=["type 2 diabetes", "diabetes mellitus"],
        topics=["endocrinology"],
        url="https://guidelines.ecri.org/example-diabetes",
    )


async def _create_inactive_guideline(suffix: str) -> ECRIGuideline:
    return await ECRIGuideline.objects.acreate(
        guideline_id=f"test-inactive-{suffix}",
        title="Old Cardiac Guideline",
        procedure_keywords=["pci"],
        diagnosis_keywords=["coronary artery disease"],
        is_active=False,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_citation_string_includes_org_year_and_url():
    guideline = await _create_cardiac_guideline("citation-string")
    citation = guideline.citation_string()
    assert "Chronic Coronary Disease Management" in citation
    assert "ACC/AHA" in citation
    assert "2023" in citation
    assert "https://guidelines.ecri.org/example-cad" in citation
    assert "ECRI Guidelines Trust" in citation


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_returns_empty_with_no_inputs():
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="", diagnosis=""
    )
    assert results == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_matches_on_procedure_keyword():
    cardiac = await _create_cardiac_guideline("match-procedure")
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="Coronary Angiography",
        diagnosis="",
    )
    assert any(g.guideline_id == cardiac.guideline_id for g in results)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_matches_on_diagnosis_keyword():
    cardiac = await _create_cardiac_guideline("match-diagnosis")
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="",
        diagnosis="stable angina",
    )
    assert any(g.guideline_id == cardiac.guideline_id for g in results)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_excludes_inactive():
    cardiac = await _create_cardiac_guideline("excludes-inactive")
    inactive = await _create_inactive_guideline("excludes-inactive")
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="pci",
        diagnosis="coronary artery disease",
        max_results=10,
    )
    ids = {g.guideline_id for g in results}
    assert cardiac.guideline_id in ids
    assert inactive.guideline_id not in ids


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_max_results_respected():
    await _create_cardiac_guideline("max-results")
    await _create_diabetes_guideline("max-results")
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="pci cgm",
        diagnosis="",
        max_results=1,
    )
    assert len(results) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_no_match_returns_empty():
    await _create_cardiac_guideline("no-match")
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="zzz_unmatched_procedure_zzz",
        diagnosis="zzz_unmatched_diagnosis_zzz",
    )
    assert results == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_find_relevant_guidelines_matches_two_letter_acronym():
    """Common clinical acronyms (RA, MI, CT) must still match keywords."""
    ra_guideline = await ECRIGuideline.objects.acreate(
        guideline_id="test-ra-acronym",
        title="Rheumatoid Arthritis Guideline",
        diagnosis_keywords=["ra", "rheumatoid arthritis"],
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="",
        diagnosis="RA",
    )
    assert any(g.guideline_id == ra_guideline.guideline_id for g in results)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_get_citations_formats_each_match():
    await _create_cardiac_guideline("citations-format")
    citations = await ECRIGuidelinesHelper.get_citations(
        procedure="coronary angiography",
        diagnosis="stable angina",
    )
    assert any("Chronic Coronary Disease Management" in c for c in citations)
    assert any("ECRI Guidelines Trust" in c for c in citations)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_get_context_includes_summary_and_url():
    await _create_cardiac_guideline("context-summary")
    context = await ECRIGuidelinesHelper.get_context(
        procedure="coronary angiography",
        diagnosis="stable angina",
    )
    assert "Evidence-Based Clinical Practice Guidelines" in context
    assert "Chronic Coronary Disease Management" in context
    assert "guideline-directed medical therapy" in context
    assert "https://guidelines.ecri.org/example-cad" in context


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_get_context_truncates_long_summary():
    await ECRIGuideline.objects.acreate(
        guideline_id="test-long-truncate",
        title="Big Guideline",
        recommendations_summary="X" * 5000,
        procedure_keywords=["bigprocxyz"],
        diagnosis_keywords=["bigdxxyz"],
    )
    context = await ECRIGuidelinesHelper.get_context(
        procedure="bigprocxyz",
        diagnosis="bigdxxyz",
        max_chars_per_guideline=200,
    )
    assert "..." in context
    assert "X" * 250 not in context


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_get_context_empty_when_no_match():
    context = await ECRIGuidelinesHelper.get_context(
        procedure="zzz_truly_unmatched_procedure_zzz",
        diagnosis="zzz_truly_unmatched_diagnosis_zzz",
    )
    assert context == ""

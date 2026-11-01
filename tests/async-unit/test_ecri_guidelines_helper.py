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
async def test_short_token_does_not_match_substring_inside_word():
    """`ra` (a substring of `trastuzumab`) must NOT match unrelated keywords."""
    breast_cancer = await ECRIGuideline.objects.acreate(
        guideline_id="test-breast-cancer-substring",
        title="HER2 Breast Cancer Guideline",
        procedure_keywords=["trastuzumab", "pertuzumab"],
        diagnosis_keywords=["her2 positive breast cancer"],
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="",
        diagnosis="ra",
    )
    assert all(
        g.guideline_id != breast_cancer.guideline_id for g in results
    ), "ra should not match trastuzumab via substring scan"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_topics_alone_do_not_drive_matches():
    """Topic labels are too broad and should not produce matches by themselves."""
    cardio = await ECRIGuideline.objects.acreate(
        guideline_id="test-topic-only",
        title="Cardio Guideline",
        topics=["cardiology"],
        # No procedure/diagnosis keywords; only topic.
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="cardiology",
        diagnosis="",
    )
    assert all(g.guideline_id != cardio.guideline_id for g in results)


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
async def test_one_shared_word_is_not_a_match():
    """The NCD lookup's rule: "stable angina" shares only "angina" with an
    "unstable angina" keyword, and a knee injection shares only "knee" with
    a knee arthroplasty one."""
    knee = await ECRIGuideline.objects.acreate(
        guideline_id="test-one-word-knee",
        title="Knee Arthroplasty Guideline",
        procedure_keywords=["knee arthroplasty"],
        diagnosis_keywords=["unstable angina"],
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="knee pain injection",
        diagnosis="stable angina",
    )
    assert all(g.guideline_id != knee.guideline_id for g in results)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_shared_generic_word_is_not_a_match():
    """"therapy" and "analysis" name a kind of work, not its subject."""
    pt = await ECRIGuideline.objects.acreate(
        guideline_id="test-generic-word",
        title="Physical Therapy Guideline",
        procedure_keywords=["physical therapy", "gait analysis"],
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="speech therapy",
        diagnosis="applied behavior analysis",
    )
    assert all(g.guideline_id != pt.guideline_id for g in results)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_plural_query_still_matches_singular_keyword():
    stent = await ECRIGuideline.objects.acreate(
        guideline_id="test-plural-stent",
        title="Coronary Stent Guideline",
        procedure_keywords=["coronary stent"],
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="coronary stents",
        diagnosis="",
    )
    assert any(g.guideline_id == stent.guideline_id for g in results)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_closer_match_ranks_first():
    """A guideline whose phrase the query covers in full comes ahead of a
    newer one that only covers most of the query."""
    close = await ECRIGuideline.objects.acreate(
        guideline_id="test-rank-close",
        title="Lumbar Fusion Guideline",
        publication_date=date(2020, 1, 1),
        procedure_keywords=["lumbar spinal fusion"],
    )
    partial = await ECRIGuideline.objects.acreate(
        guideline_id="test-rank-partial",
        title="Spinal Fusion Guideline",
        publication_date=date(2025, 1, 1),
        procedure_keywords=["cervical spinal fusion"],
    )
    results = await ECRIGuidelinesHelper.find_relevant_guidelines(
        procedure="lumbar spinal fusion",
        diagnosis="",
        max_results=5,
    )
    ids = [g.guideline_id for g in results]
    assert ids.index(close.guideline_id) < ids.index(partial.guideline_id)

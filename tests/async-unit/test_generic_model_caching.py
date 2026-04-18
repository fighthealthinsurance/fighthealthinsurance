import pytest
from unittest import mock

from fighthealthinsurance.models import (
    GenericQuestionGeneration,
    GenericContextGeneration,
)
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
import traceback


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_generic_question_generation_cache():
    """Test that generic questions are cached and reused properly."""
    # Mock data
    procedure = "knee replacement"
    diagnosis = "osteoarthritis"
    mock_questions = [("Question 1?", ""), ("Question 2?", "")]
    mock_questions_lst = [["Question 1?", ""], ["Question 2?", ""]]

    # Mock the ML model to avoid actual ML calls
    with mock.patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    ) as mock_full_qa_backends, mock.patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    ) as mock_partial_qa_backends:

        # Create a mock model with the get_appeal_questions method
        mock_model = mock.AsyncMock()
        mock_model.get_appeal_questions.return_value = mock_questions

        # Configure the mock_full_qa_backends to return our mock model
        mock_full_qa_backends.return_value = [mock_model]
        mock_partial_qa_backends.return_value = [mock_model]

        # First call should create a new cache entry
        result1 = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=procedure, diagnosis=diagnosis
        )

        # Verify the result matches our mock data
        assert result1 == mock_questions

        # Verify the ML model was called once
        mock_model.get_appeal_questions.assert_called_once()

        # Reset the mocks to verify they're not called again
        mock_model.get_appeal_questions.reset_mock()

        # Second call should use cached data
        result2 = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=procedure, diagnosis=diagnosis
        )

        # Verify the result is the same
        assert result2 == mock_questions_lst

        # Verify the ML model was NOT called again
        mock_model.get_appeal_questions.assert_not_called()

        # Verify the database has one cache entry
        cache_entry = await GenericQuestionGeneration.objects.filter(
            procedure=procedure, diagnosis=diagnosis
        ).afirst()

        assert cache_entry is not None
        assert cache_entry.generated_questions == mock_questions_lst


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_generic_context_generation_cache():
    """Test that generic context/citations are cached and reused properly."""
    # Mock data
    procedure = "knee replacement"
    diagnosis = "osteoarthritis"
    mock_citations = ["Citation 1", "Citation 2", "Citation 3"]

    # Mock the citation backends to avoid actual ML calls
    with mock.patch(
        "fighthealthinsurance.ml.ml_citations_helper.ml_router"
    ) as mock_router, mock.patch(
        "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit"
    ) as mock_best_within_timelimit:
        # Setup mock backends that return our predefined citations
        mock_backend = mock.MagicMock()
        mock_backend.get_citations.return_value = mock_citations
        mock_router.partial_find_citation_backends.return_value = [mock_backend]
        mock_best_within_timelimit.return_value = mock_citations

        # First call should create a new cache entry
        result1 = await MLCitationsHelper.generate_generic_citations(
            procedure_opt=procedure, diagnosis_opt=diagnosis
        )

        # Verify the result matches our mock data
        assert result1 == mock_citations

        # Verify the ML backend was called once
        mock_backend.get_citations.assert_called_once()

        # Reset the backend mock to verify it's not called again
        mock_backend.get_citations.reset_mock()
        mock_best_within_timelimit.reset_mock()

        # Second call should use cached data
        result2 = await MLCitationsHelper.generate_generic_citations(
            procedure_opt=procedure, diagnosis_opt=diagnosis
        )

        # Verify the result is the same
        assert result2 == mock_citations

        # Verify the ML backend was NOT called again
        mock_backend.get_citations.assert_not_called()
        mock_best_within_timelimit.assert_not_called()

        # Verify the database has one cache entry
        cache_entry = await GenericContextGeneration.objects.filter(
            procedure=procedure, diagnosis=diagnosis
        ).afirst()

        assert cache_entry is not None
        assert cache_entry.generated_context == mock_citations


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_denial_uses_generic_cache_no_patient_data():
    """Test that a denial without patient-specific data uses cached generic questions/context."""
    from fighthealthinsurance.models import Denial

    # Create a test denial with only procedure and diagnosis (no patient data)
    test_denial = await Denial.objects.acreate(
        procedure="knee replacement",
        diagnosis="osteoarthritis",
        denial_text="",  # Empty to trigger generic path
        health_history="",  # Empty to trigger generic path
        use_external=False,
    )

    # Mock cached data
    mock_questions = [("Question 1?", ""), ("Question 2?", "")]
    mock_citations = ["Citation 1", "Citation 2", "Citation 3"]

    # Create cache entries
    await GenericQuestionGeneration.objects.acreate(
        procedure="knee replacement",
        diagnosis="osteoarthritis",
        generated_questions=mock_questions,
    )

    await GenericContextGeneration.objects.acreate(
        procedure="knee replacement",
        diagnosis="osteoarthritis",
        generated_context=mock_citations,
    )

    # Mock to ensure we don't make actual ML calls
    with mock.patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    ) as mock_qa_backends, mock.patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    ) as mock_partial_qa_backends, mock.patch(
        "fighthealthinsurance.ml.ml_citations_helper.ml_router"
    ) as mock_router:

        # Configure the mock backends to return an empty list to ensure no models are called
        mock_qa_backends.return_value = []
        mock_partial_qa_backends.return_value = []

        # Generate questions for denial
        questions = await MLAppealQuestionsHelper.generate_questions_for_denial(
            denial=test_denial, speculative=False
        )

        # Generate citations for denial
        citations = await MLCitationsHelper.generate_citations_for_denial(
            denial=test_denial, speculative=False
        )

        # Verify we got the cached data - use list comparison instead of set to avoid unhashable type error
        assert len(questions) == len(mock_questions)
        for q in questions:
            assert (q[0], q[1]) in mock_questions

        assert len(citations) == len(mock_citations)
        for c in citations:
            assert c in mock_citations

        # Verify the ML models were NOT called - we're using cached entries
        # We expect full_qa_backends to be called twice (once for generic and once for specific questions)
        assert mock_qa_backends.call_count <= 2
        mock_router.partial_find_citation_backends.assert_not_called()

    # Cleanup
    await test_denial.adelete()

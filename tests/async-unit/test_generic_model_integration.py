import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fighthealthinsurance.models import (
    Denial,
    GenericQuestionGeneration,
    GenericContextGeneration,
)


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_end_to_end_generic_cache_workflow():
    """Test the complete workflow from empty cache to cached results."""
    procedure = "physical therapy"
    diagnosis = "lower back pain"

    # 1. First ensure our cache is empty for this procedure/diagnosis
    await GenericQuestionGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).adelete()
    await GenericContextGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).adelete()

    # 2. Create a test denial with no patient-specific data
    test_denial = await Denial.objects.acreate(
        procedure=procedure,
        diagnosis=diagnosis,
        denial_text="",
        health_history="",
    )

    # Mock the question generation and citation generation to verify calls
    mock_questions = [("What is the medical necessity?", "Answer about necessity")]
    mock_citations = ["Study shows efficacy of physical therapy for lower back pain"]

    # Set up mocks
    with patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    ) as mock_qa_backends, patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    ) as mock_partial_qa_backends, patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_find_citation_backends"
    ) as mock_citation_backends, patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_find_citation_backends"
    ) as mock_partial_citation_backends:

        # Configure question mock
        mock_question_model = AsyncMock()
        mock_question_model.get_appeal_questions = AsyncMock(
            return_value=mock_questions
        )
        mock_qa_backends.return_value = [mock_question_model]
        mock_partial_qa_backends.return_value = [mock_question_model]

        # Configure citation mock
        mock_citation_model = MagicMock()
        mock_citation_model.get_citations = AsyncMock(return_value=mock_citations)
        mock_partial_citation_backends.return_value = [mock_citation_model]
        mock_citation_backends.return_value = [mock_citation_model]

        # Import here to avoid circular import issues
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            MLAppealQuestionsHelper,
        )
        from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper

        # 3. First run - should generate and cache
        questions1 = await MLAppealQuestionsHelper.generate_questions_for_denial(
            denial=test_denial, speculative=False
        )

        # Verify ML model was called
        assert mock_question_model.get_appeal_questions.call_count <= 2

        citations1 = await MLCitationsHelper.generate_citations_for_denial(
            denial=test_denial, speculative=False
        )

        assert mock_citation_model.get_citations.call_count <= 2

        # Reset mocks to verify they're not called again
        mock_question_model.get_appeal_questions.reset_mock()
        mock_citation_model.get_citations.reset_mock()

        # 4. Verify cache entries were created
        question_cache = await GenericQuestionGeneration.objects.filter(
            procedure=procedure, diagnosis=diagnosis
        ).afirst()
        assert question_cache is not None

        context_cache = await GenericContextGeneration.objects.filter(
            procedure=procedure, diagnosis=diagnosis
        ).afirst()
        assert context_cache is not None

        # 5. Create a second denial with the same procedure/diagnosis
        test_denial2 = await Denial.objects.acreate(
            procedure=procedure,
            diagnosis=diagnosis,
            denial_text="",
            health_history="",
        )

        # 6. Second run - should use cache
        questions2 = await MLAppealQuestionsHelper.generate_questions_for_denial(
            denial=test_denial2, speculative=False
        )
        citations2 = await MLCitationsHelper.generate_citations_for_denial(
            denial=test_denial2, speculative=False
        )

        # Verify ML model was NOT called
        mock_question_model.get_appeal_questions.assert_not_called()
        mock_citation_model.get_citations.assert_not_called()

    # 7. Cleanup test data
    await test_denial.adelete()
    await test_denial2.adelete()
    await GenericQuestionGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).adelete()
    await GenericContextGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).adelete()

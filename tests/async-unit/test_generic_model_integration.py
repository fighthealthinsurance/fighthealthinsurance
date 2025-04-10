import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fighthealthinsurance.models import (
    Denial,
    GenericQuestionGeneration,
    GenericContextGeneration,
)


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
        "fighthealthinsurance.ml.ml_appeal_questions_helper.AppealGenerator"
    ) as MockAppealGenerator, patch(
        "fighthealthinsurance.ml.ml_citations_helper.MLCitationsHelper.ml_router"
    ) as mock_router:

        # Configure question mock
        mock_appeal_gen = MockAppealGenerator.return_value
        mock_appeal_gen.get_appeal_questions = AsyncMock(return_value=mock_questions)

        # Configure citation mock
        mock_backend = MagicMock()
        mock_backend.get_citations = AsyncMock(return_value=mock_citations)
        mock_router.partial_find_citation_backends.return_value = [mock_backend]

        # Import here to avoid circular import issues
        from fighthealthinsurance.ml.ml_appeal_questions_helper import (
            MLAppealQuestionsHelper,
        )
        from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper

        # 3. First run - should generate and cache
        questions1 = await MLAppealQuestionsHelper.generate_questions_for_denial(
            denial=test_denial, speculative=False
        )
        citations1 = await MLCitationsHelper.generate_citations_for_denial(
            denial=test_denial, speculative=False
        )

        # Verify ML model was called
        mock_appeal_gen.get_appeal_questions.assert_called_once()
        mock_backend.get_citations.assert_called_once()

        # Reset mocks to verify they're not called again
        mock_appeal_gen.get_appeal_questions.reset_mock()
        mock_backend.get_citations.reset_mock()

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
        mock_appeal_gen.get_appeal_questions.assert_not_called()
        mock_backend.get_citations.assert_not_called()

        # Verify cached results were returned
        assert questions2 == mock_questions
        assert citations2 == mock_citations

    # 7. Cleanup test data
    await test_denial.adelete()
    await test_denial2.adelete()
    await GenericQuestionGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).adelete()
    await GenericContextGeneration.objects.filter(
        procedure=procedure, diagnosis=diagnosis
    ).adelete()

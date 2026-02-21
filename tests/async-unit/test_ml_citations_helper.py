from unittest.mock import patch, AsyncMock, MagicMock
import pytest

from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.models import Denial


class TestMLCitationsHelper:
    """Tests for the MLCitationsHelper class.

    Note: These tests mock the module-level ml_router and database access.
    The API has changed from earlier versions - generate_citations() no longer
    exists, replaced by generate_generic_citations() and generate_specific_citations().
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_backend = AsyncMock()
        self.mock_backend.get_citations.return_value = ["Citation 1", "Citation 2"]

        # Create a mock denial
        self.mock_denial = MagicMock(spec=Denial)
        self.mock_denial.denial_id = 12345
        self.mock_denial.denial_text = "Test denial text"
        self.mock_denial.procedure = "Test procedure"
        self.mock_denial.diagnosis = "Test diagnosis"
        self.mock_denial.health_history = "Test health history"
        self.mock_denial.plan_context = "Test plan context"
        self.mock_denial.use_external = False
        self.mock_denial.ml_citation_context = None
        self.mock_denial.candidate_ml_citation_context = None
        self.mock_denial.candidate_procedure = None
        self.mock_denial.candidate_diagnosis = None
        self.mock_denial.microsite_slug = None

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_generate_specific_citations(self, mock_ml_router):
        """Test generate_specific_citations method."""
        mock_ml_router.full_find_citation_backends.return_value = [self.mock_backend]

        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=["Citation 1", "Citation 2"],
        ):
            citations = await MLCitationsHelper.generate_specific_citations(
                denial=self.mock_denial
            )

            assert len(citations) == 2
            assert citations[0] == "Citation 1"

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_generate_specific_citations_no_backends(self, mock_ml_router):
        """Test behavior when no backends are available."""
        mock_ml_router.full_find_citation_backends.return_value = []

        citations = await MLCitationsHelper.generate_specific_citations(
            denial=self.mock_denial
        )

        assert citations == []

    @pytest.mark.asyncio
    async def test_generate_specific_citations_no_context(self):
        """Test that generate_specific_citations returns empty when no context."""
        # Create denial with no patient-specific context
        denial_no_context = MagicMock(spec=Denial)
        denial_no_context.denial_text = None
        denial_no_context.plan_context = None
        denial_no_context.health_history = None
        denial_no_context.procedure = "test"
        denial_no_context.diagnosis = "test"

        citations = await MLCitationsHelper.generate_specific_citations(
            denial=denial_no_context
        )

        # Should return empty since no patient-specific context
        assert citations == []

    @pytest.mark.asyncio
    async def test_generate_citations_for_denial_existing_citations(self):
        """Test behavior when citations already exist for the denial."""
        # Setup - denial already has citation context
        denial_with_citations = MagicMock(spec=Denial)
        denial_with_citations.denial_id = 54321
        denial_with_citations.ml_citation_context = [
            "Existing citation 1",
            "Existing citation 2",
        ]

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            denial_with_citations, speculative=False
        )

        # Verify existing citations were returned
        assert result == ["Existing citation 1", "Existing citation 2"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.Denial.objects")
    async def test_generate_citations_for_denial_use_candidate(self, mock_denial_objects):
        """Test using candidate citations when they exist with matching procedure/diagnosis."""
        # Setup mock for database update
        mock_queryset = MagicMock()
        mock_queryset.aupdate = AsyncMock()
        mock_denial_objects.filter.return_value = mock_queryset

        # Setup - denial has candidate citations with matching procedure/diagnosis
        denial_with_candidates = MagicMock(spec=Denial)
        denial_with_candidates.denial_id = 66666
        denial_with_candidates.procedure = "Test procedure"
        denial_with_candidates.diagnosis = "Test diagnosis"
        denial_with_candidates.ml_citation_context = None
        denial_with_candidates.candidate_ml_citation_context = [
            "Candidate citation 1",
            "Candidate citation 2",
        ]
        denial_with_candidates.candidate_procedure = "Test procedure"
        denial_with_candidates.candidate_diagnosis = "Test diagnosis"

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            denial_with_candidates, speculative=False
        )

        # Verify candidate citations were returned
        assert result == ["Candidate citation 1", "Candidate citation 2"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.Denial.objects")
    @patch.object(MLCitationsHelper, "_generate_citations_for_denial")
    async def test_generate_citations_for_denial_stores_non_speculative(
        self, mock_generate, mock_denial_objects
    ):
        """Test that generated citations are stored in non-speculative field."""
        # Setup
        mock_generate.return_value = ["Citation A", "Citation B"]
        mock_queryset = MagicMock()
        mock_queryset.aupdate = AsyncMock()
        mock_denial_objects.filter.return_value = mock_queryset

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            self.mock_denial, speculative=False
        )

        # Verify citations were stored in non-speculative field
        mock_queryset.aupdate.assert_called_once_with(
            ml_citation_context=["Citation A", "Citation B"]
        )
        assert result == ["Citation A", "Citation B"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.Denial.objects")
    @patch.object(MLCitationsHelper, "_generate_citations_for_denial")
    async def test_generate_citations_for_denial_stores_speculative(
        self, mock_generate, mock_denial_objects
    ):
        """Test that generated citations are stored in speculative/candidate field."""
        # Setup
        mock_generate.return_value = ["Citation X", "Citation Y"]
        mock_queryset = MagicMock()
        mock_queryset.aupdate = AsyncMock()
        mock_denial_objects.filter.return_value = mock_queryset

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            self.mock_denial, speculative=True
        )

        # Verify citations were stored in speculative field
        mock_queryset.aupdate.assert_called_once_with(
            candidate_ml_citation_context=["Citation X", "Citation Y"]
        )
        assert result == ["Citation X", "Citation Y"]

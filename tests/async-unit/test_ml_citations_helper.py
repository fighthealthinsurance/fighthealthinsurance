import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call
import pytest
import asyncio
import json

from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.models import Denial


class TestMLCitationsHelper(unittest.TestCase):
    """Tests for the MLCitationsHelper class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a mock router
        self.mock_router = MagicMock(spec=MLRouter)
        self.mock_backend = AsyncMock()
        self.mock_backend.get_citations.return_value = ["Citation 1", "Citation 2"]

        # Setup mock backends for different scenarios
        self.mock_router.full_find_citation_backends.return_value = [self.mock_backend]
        self.mock_router.partial_find_citation_backends.return_value = [
            self.mock_backend
        ]

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

    @patch.object(MLCitationsHelper, "ml_router", new_callable=MagicMock)
    async def test_generate_citations(self, mock_ml_router):
        """Test the generate_citations method."""
        # Setup
        mock_ml_router.full_find_citation_backends.return_value = [self.mock_backend]
        mock_ml_router.partial_find_citation_backends.return_value = [self.mock_backend]

        # Execute
        citations = await MLCitationsHelper.generate_citations(
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            patient_context="Test patient context",
            plan_context="Test plan context",
            use_external=True,
        )

        # Verify
        self.assertEqual(len(citations), 2)
        self.assertEqual(citations[0], "Citation 1")
        self.assertEqual(citations[1], "Citation 2")
        mock_ml_router.full_find_citation_backends.assert_called_once_with(
            use_external=True
        )
        self.mock_backend.get_citations.assert_called_once_with(
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            patient_context="Test patient context",
            plan_context="Test plan context",
        )

    @patch.object(MLCitationsHelper, "ml_router", new_callable=MagicMock)
    async def test_generate_citations_full_backend_failure(self, mock_ml_router):
        """Test handling a failure in the full backend."""
        # Setup - make the full backend fail
        mock_full_backend = AsyncMock()
        mock_full_backend.get_citations.side_effect = Exception("Backend error")

        # Partial backend succeeds
        mock_partial_backend = AsyncMock()
        mock_partial_backend.get_citations.return_value = ["Backup citation 1"]

        mock_ml_router.full_find_citation_backends.return_value = [mock_full_backend]
        mock_ml_router.partial_find_citation_backends.return_value = [
            mock_partial_backend
        ]

        # Execute
        citations = await MLCitationsHelper.generate_citations(
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            use_external=True,
        )

        # Verify we got results from partial backend
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0], "Backup citation 1")

        # Verify both backends were attempted
        mock_full_backend.get_citations.assert_called_once()
        mock_partial_backend.get_citations.assert_called_once()

    @patch.object(MLCitationsHelper, "ml_router", new_callable=MagicMock)
    async def test_generate_citations_no_backends(self, mock_ml_router):
        """Test behavior when no backends are available."""
        # Setup - no backends available
        mock_ml_router.full_find_citation_backends.return_value = []
        mock_ml_router.partial_find_citation_backends.return_value = []

        # Execute
        citations = await MLCitationsHelper.generate_citations(
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify empty result
        self.assertEqual(citations, [])

    @patch.object(MLCitationsHelper, "generate_citations")
    @patch("fighthealthinsurance.models.Denial.objects.filter")
    async def test_generate_citations_for_denial_non_speculative(
        self, mock_filter, mock_generate_citations
    ):
        """Test generating citations for a denial in non-speculative mode."""
        # Setup
        mock_denial_queryset = AsyncMock()
        mock_denial_queryset.aupdate = AsyncMock()
        mock_filter.return_value = mock_denial_queryset

        mock_generate_citations.return_value = ["Citation A", "Citation B"]

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            self.mock_denial, speculative=False
        )

        # Verify
        mock_generate_citations.assert_called_once_with(
            denial_text=self.mock_denial.denial_text,
            procedure=self.mock_denial.procedure,
            diagnosis=self.mock_denial.diagnosis,
            patient_context=self.mock_denial.health_history,
            plan_context=self.mock_denial.plan_context,
            use_external=self.mock_denial.use_external,
        )

        # Verify citations were stored in non-speculative field
        mock_denial_queryset.aupdate.assert_called_once_with(
            ml_citation_context=["Citation A", "Citation B"]
        )

        # Verify the result
        self.assertEqual(result, ["Citation A", "Citation B"])

    @patch.object(MLCitationsHelper, "generate_citations")
    @patch("fighthealthinsurance.models.Denial.objects.filter")
    async def test_generate_citations_for_denial_speculative(
        self, mock_filter, mock_generate_citations
    ):
        """Test generating citations for a denial in speculative mode."""
        # Setup
        mock_denial_queryset = AsyncMock()
        mock_denial_queryset.aupdate = AsyncMock()
        mock_filter.return_value = mock_denial_queryset

        mock_generate_citations.return_value = ["Citation X", "Citation Y"]

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            self.mock_denial, speculative=True
        )

        # Verify
        mock_generate_citations.assert_called_once_with(
            denial_text=self.mock_denial.denial_text,
            procedure=self.mock_denial.procedure,
            diagnosis=self.mock_denial.diagnosis,
            patient_context=self.mock_denial.health_history,
            plan_context=self.mock_denial.plan_context,
            use_external=self.mock_denial.use_external,
        )

        # Verify citations were stored in speculative field
        mock_denial_queryset.aupdate.assert_called_once_with(
            candidate_ml_citation_context=["Citation X", "Citation Y"]
        )

        # Verify the result
        self.assertEqual(result, ["Citation X", "Citation Y"])

    @patch.object(MLCitationsHelper, "generate_citations")
    async def test_generate_citations_for_denial_existing_citations(
        self, mock_generate_citations
    ):
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

        # Verify no generation was attempted
        mock_generate_citations.assert_not_called()

        # Verify existing citations were returned
        self.assertEqual(result, ["Existing citation 1", "Existing citation 2"])

    @patch.object(MLCitationsHelper, "generate_citations")
    async def test_generate_citations_for_denial_use_candidate(
        self, mock_generate_citations
    ):
        """Test using candidate citations when they exist with matching procedure/diagnosis."""
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

        # Verify no generation was attempted since candidates are used
        mock_generate_citations.assert_not_called()

        # Verify candidate citations were returned and stored
        self.assertEqual(result, ["Candidate citation 1", "Candidate citation 2"])

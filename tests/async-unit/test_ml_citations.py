import unittest
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import asyncio

from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.ml.ml_models import (
    RemoteModelLike,
    RemoteFullOpenLike,
    RemotePerplexity,
    DeepInfra,
)


class TestMLCitationFunctionality(unittest.TestCase):
    """Tests for the citation functionality in ML models."""

    def setUp(self):
        # Create a mock router
        self.router = MLRouter()

        # Create a mock model instance
        self.mock_model = AsyncMock(spec=RemoteFullOpenLike)
        self.mock_model.get_citations = AsyncMock(
            return_value=["Citation 1", "Citation 2"]
        )
        self.mock_model._infer = AsyncMock(return_value="Citation 1\nCitation 2")

        # Create a test system prompt map
        self.system_prompts_map = {
            "citations": ["You are a helpful assistant providing citations."]
        }

    @patch("fighthealthinsurance.ml.ml_models.RemoteFullOpenLike.get_system_prompts")
    @patch("fighthealthinsurance.ml.ml_models.RemoteFullOpenLike._infer")
    async def test_get_citations_with_all_params(
        self, mock_infer, mock_get_system_prompts
    ):
        """Test citation generation with all parameters provided."""
        # Setup mocks
        mock_infer.return_value = "Citation 1\nCitation 2\nCitation 3"
        mock_get_system_prompts.return_value = [
            "You are a helpful assistant providing citations."
        ]

        # Create model instance
        model = RemoteFullOpenLike("http://test-api.com", "test-token", "test-model")

        # Call get_citations with all parameters
        citations = await model.get_citations(
            denial_text="This is a denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            patient_context="Patient history",
            plan_context="Plan details",
            pubmed_context="PubMed research",
        )

        # Verify results
        self.assertEqual(len(citations), 3)
        self.assertEqual(citations[0], "Citation 1")

        # Verify _infer was called with the right parameters
        mock_infer.assert_called_once()
        call_args = mock_infer.call_args[1]
        self.assertIn("temperature", call_args)
        self.assertEqual(
            call_args["temperature"], 0.25
        )  # Verify lower temperature is used

        # Verify prompt construction
        prompt = call_args["prompt"]
        self.assertIn("Denial text: This is a denial", prompt)
        self.assertIn("The procedure denied was Test procedure", prompt)
        self.assertIn("The primary diagnosis was Test diagnosis", prompt)
        self.assertIn("Patient history: Patient history", prompt)
        self.assertIn("Available research: PubMed research", prompt)

    @patch("fighthealthinsurance.ml.ml_models.RemoteFullOpenLike.get_system_prompts")
    @patch("fighthealthinsurance.ml.ml_models.RemoteFullOpenLike._infer")
    async def test_get_citations_with_minimal_params(
        self, mock_infer, mock_get_system_prompts
    ):
        """Test citation generation with minimal parameters (no denial text)."""
        # Setup mocks
        mock_infer.return_value = "Citation 1\nCitation 2"
        mock_get_system_prompts.return_value = [
            "You are a helpful assistant providing citations."
        ]

        # Create model instance
        model = RemoteFullOpenLike("http://test-api.com", "test-token", "test-model")

        # Call get_citations with minimal parameters
        citations = await model.get_citations(
            denial_text=None, procedure="Test procedure", diagnosis="Test diagnosis"
        )

        # Verify results
        self.assertEqual(len(citations), 2)

        # Verify prompt construction
        prompt = mock_infer.call_args[1]["prompt"]
        self.assertIn("No denial text provided", prompt)
        self.assertIn("The procedure denied was Test procedure", prompt)

    @patch("fighthealthinsurance.ml.ml_models.RemoteFullOpenLike.get_system_prompts")
    @patch("fighthealthinsurance.ml.ml_models.RemoteFullOpenLike._infer")
    async def test_get_citations_parsing(self, mock_infer, mock_get_system_prompts):
        """Test citation parsing with various formats."""
        # Setup mocks with different citation formats
        mock_infer.return_value = """
        Here are relevant citations:
        1. Smith et al., "Treatment Efficacy", Journal of Medicine, 2023, DOI: 10.1234/jmed.2023
        * Jones B, Brown C. "Clinical Guidelines", Med Practice, 2022
        - PMID: 123456789
        """
        mock_get_system_prompts.return_value = [
            "You are a helpful assistant providing citations."
        ]

        # Create model instance
        model = RemoteFullOpenLike("http://test-api.com", "test-token", "test-model")

        # Call get_citations
        citations = await model.get_citations(
            denial_text="This is a denial", procedure="Test procedure", diagnosis=None
        )

        # Verify parsing correctly handles different formats
        self.assertEqual(len(citations), 3)
        self.assertIn(
            """Smith et al., "Treatment Efficacy", Journal of Medicine, 2023, DOI: 10.1234/jmed.2023""",
            citations,
        )
        self.assertIn(
            """Jones B, Brown C. "Clinical Guidelines", Med Practice, 2022""", citations
        )
        self.assertIn("""PMID: 123456789""", citations)

        # Verify header line is excluded
        for citation in citations:
            self.assertNotIn("Here are relevant citations", citation)

    @patch("fighthealthinsurance.ml.ml_router.MLRouter.models_by_name")
    def test_full_find_citation_backends(self, mock_models_by_name):
        """Test the full_find_citation_backends router method."""
        # Setup mocks
        mock_perplexity_model = MagicMock(spec=RemotePerplexity)
        mock_models_by_name.get.return_value = {
            "sonar-reasoning": [mock_perplexity_model],
            "deepseek": [mock_perplexity_model],
        }

        # Create router instance
        router = MLRouter()
        router.models_by_name = mock_models_by_name

        # Test with external=False
        backends = router.full_find_citation_backends(use_external=False)
        self.assertEqual(len(backends), 0)

        # Test with external=True
        backends = router.full_find_citation_backends(use_external=True)
        self.assertGreater(len(backends), 0)

    @patch("fighthealthinsurance.ml.ml_router.MLRouter.models_by_name")
    def test_partial_find_citation_backends(self, mock_models_by_name):
        """Test the partial_find_citation_backends router method."""
        # Setup mocks
        mock_perplexity_model = MagicMock(spec=RemotePerplexity)
        mock_models_by_name.get.return_value = {
            "sonar-reasoning": [mock_perplexity_model],
            "deepseek": [mock_perplexity_model],
        }

        # Create router instance
        router = MLRouter()
        router.models_by_name = mock_models_by_name

        # Test partial backends (should always return models)
        backends = router.partial_find_citation_backends()
        self.assertGreater(len(backends), 0)

from unittest.mock import patch, AsyncMock, MagicMock
import pytest

from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.ml.ml_models import (
    RemoteModelLike,
    RemoteFullOpenLike,
    RemotePerplexity,
    DeepInfra,
)


class TestMLCitationFunctionality:
    """Tests for the citation functionality in ML models."""

    @pytest.fixture(autouse=True)
    def setup(self):
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

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Test was never running - mocking does not work with instance methods")
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
        assert len(citations) == 3
        assert citations[0] == "Citation 1"

        # Verify _infer was called with the right parameters
        mock_infer.assert_called_once()
        call_args = mock_infer.call_args[1]
        assert "temperature" in call_args
        assert call_args["temperature"] == 0.25  # Verify lower temperature is used

        # Verify prompt construction
        prompt = call_args["prompt"]
        assert "Denial text: This is a denial" in prompt
        assert "The procedure denied was Test procedure" in prompt
        assert "The primary diagnosis was Test diagnosis" in prompt
        assert "Patient history: Patient history" in prompt
        assert "Available research: PubMed research" in prompt

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Test was never running - mocking does not work with instance methods")
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
        assert len(citations) == 2

        # Verify prompt construction
        prompt = mock_infer.call_args[1]["prompt"]
        assert "No denial text provided" in prompt
        assert "The procedure denied was Test procedure" in prompt

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Test was never running - mocking does not work with instance methods")
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
        assert len(citations) == 3
        assert (
            """Smith et al., "Treatment Efficacy", Journal of Medicine, 2023, DOI: 10.1234/jmed.2023"""
            in citations
        )
        assert (
            """Jones B, Brown C. "Clinical Guidelines", Med Practice, 2022"""
            in citations
        )
        assert """PMID: 123456789""" in citations

        # Verify header line is excluded
        for citation in citations:
            assert "Here are relevant citations" not in citation

    def test_full_find_citation_backends(self):
        """Test the full_find_citation_backends router method."""
        # Setup mocks
        mock_perplexity_model = MagicMock(spec=RemotePerplexity)

        # Create router instance
        router = MLRouter()
        router.models_by_name = {
            "sonar-reasoning": [mock_perplexity_model],
            "sonar": [mock_perplexity_model],
            "deepseek": [mock_perplexity_model],
        }

        # Test with external=False
        backends = router.full_find_citation_backends(use_external=False)
        assert len(backends) == 0

        # Test with external=True
        backends = router.full_find_citation_backends(use_external=True)
        assert len(backends) > 0

    def test_partial_find_citation_backends(self):
        """Test the partial_find_citation_backends router method."""
        # Setup mocks
        mock_perplexity_model = MagicMock(spec=RemotePerplexity)

        # Create router instance
        router = MLRouter()
        router.models_by_name = {
            "sonar-reasoning": [mock_perplexity_model],
            "sonar": [mock_perplexity_model],
            "deepseek": [mock_perplexity_model],
        }

        # Test partial backends (should always return models)
        backends = router.partial_find_citation_backends()
        assert len(backends) > 0

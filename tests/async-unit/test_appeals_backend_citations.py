import unittest
from unittest.mock import patch, AsyncMock, MagicMock, ANY
import pytest
import asyncio
import json
import time
from datetime import timedelta

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper


class TestAppealsBackendHelperWithCitations(unittest.TestCase):
    """Tests for the AppealsBackendHelper class with ML citations integration."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock denial
        self.mock_denial = MagicMock()
        self.mock_denial.denial_id = 12345
        self.mock_denial.denial_text = "Test denial text"
        self.mock_denial.procedure = "Test procedure"
        self.mock_denial.diagnosis = "Test diagnosis"
        self.mock_denial.health_history = "Test health history"
        self.mock_denial.insurance_company = "Test Insurance"
        self.mock_denial.claim_id = "CLAIM123"
        self.mock_denial.date = "2025-01-01"
        self.mock_denial.ml_citation_context = None
        self.mock_denial.pubmed_context = None

        # Mock the appeal generator
        self.mock_appeal_generator = MagicMock()
        self.mock_appeal_generator.make_appeals.return_value = [
            "Appeal text with citation context"
        ]

    @patch(
        "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial"
    )
    @patch("fighthealthinsurance.common_view_logic.PubMedTools.find_context_for_denial")
    @patch("fighthealthinsurance.common_view_logic.sync_to_async")
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    @patch("fighthealthinsurance.common_view_logic.a")
    async def test_generate_appeals_with_ml_citations(
        self,
        mock_a,
        mock_appeal_generator,
        mock_sync_to_async,
        mock_find_pubmed_context,
        mock_generate_citations,
    ):
        """Test appeal generation with ML citations context."""
        # Setup mocks
        mock_generate_citations.return_value = ["ML Citation 1", "ML Citation 2"]
        mock_find_pubmed_context.return_value = "PubMed context data"

        # Mock sync_to_async's behavior for appealGenerator.make_appeals
        mock_sync_to_async.return_value = MagicMock(return_value=["Appeal text 1"])

        # Mock a.map for async iteration
        mock_a.map.return_value = AsyncMock()

        # Create the parameters dictionary
        parameters = {
            "denial_id": str(self.mock_denial.denial_id),
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        # Create a generator function to yield from
        async def mock_yield():
            yield "Appeal JSON data"

        # Set up interleave_iterator to return our mock generator
        with patch(
            "fighthealthinsurance.common_view_logic.interleave_iterator_for_keep_alive",
            return_value=mock_yield(),
        ):
            # Execute
            result_generator = AppealsBackendHelper.generate_appeals(parameters)
            result = [r async for r in result_generator]

            # Verify the ML citations helper was called
            mock_generate_citations.assert_awaited_once_with(
                self.mock_denial, speculative=False
            )

            # Verify citations were passed to make_appeals
            mock_sync_to_async.return_value.assert_called_once()
            # Check that ml_citations_context was included in the call to make_appeals
            call_args = mock_sync_to_async.return_value.call_args
            # The call should pass the ML citations context to make_appeals
            self.assertIn(mock_generate_citations.return_value, call_args[0])

            # Verify we got a result
            self.assertEqual(result, ["Appeal JSON data"])

    @patch(
        "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial"
    )
    @patch("fighthealthinsurance.common_view_logic.PubMedTools.find_context_for_denial")
    async def test_pubmed_and_ml_citations_timeouts(
        self, mock_find_pubmed_context, mock_generate_citations
    ):
        """Test handling of timeouts when fetching citation contexts."""
        # Setup mocks to timeout
        mock_find_pubmed_context.side_effect = asyncio.TimeoutError("PubMed timeout")
        mock_generate_citations.side_effect = asyncio.TimeoutError(
            "ML citations timeout"
        )

        # Create the parameters dictionary
        parameters = {
            "denial_id": str(self.mock_denial.denial_id),
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        # Mock the behavior of other components to isolate the test
        with patch(
            "fighthealthinsurance.common_view_logic.Denial.objects.filter"
        ) as mock_filter:
            mock_queryset = AsyncMock()
            mock_queryset.select_related = MagicMock(return_value=mock_queryset)
            mock_queryset.aget.return_value = self.mock_denial
            mock_filter.return_value = mock_queryset

            # Mock the rest of the appeals generation process
            with patch(
                "fighthealthinsurance.common_view_logic.sync_to_async"
            ) as mock_sync_to_async:
                mock_sync_to_async.return_value = MagicMock(
                    return_value=["Appeal with no citations"]
                )

                with patch("fighthealthinsurance.common_view_logic.a") as mock_a:
                    mock_a.map.return_value = AsyncMock()

                    # Create a generator function to yield from
                    async def mock_yield():
                        yield "Appeal JSON data with no citations"

                    # Set up interleave_iterator to return our mock generator
                    with patch(
                        "fighthealthinsurance.common_view_logic.interleave_iterator_for_keep_alive",
                        return_value=mock_yield(),
                    ):
                        # Execute - should not raise exceptions despite timeouts
                        result_generator = AppealsBackendHelper.generate_appeals(
                            parameters
                        )
                        result = [r async for r in result_generator]

                        # Verify the result still works even with timeouts
                        self.assertEqual(result, ["Appeal JSON data with no citations"])

    @patch(
        "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial"
    )
    @patch("fighthealthinsurance.common_view_logic.PubMedTools.find_context_for_denial")
    @patch("fighthealthinsurance.common_view_logic.sync_to_async")
    async def test_both_citation_sources_used(
        self, mock_sync_to_async, mock_find_pubmed_context, mock_generate_citations
    ):
        """Test that both PubMed and ML citations are used when available."""
        # Setup mocks with successful returns
        mock_generate_citations.return_value = ["ML Citation 1", "ML Citation 2"]
        mock_find_pubmed_context.return_value = "PubMed context data"

        # Mock sync_to_async's behavior for appealGenerator.make_appeals
        mock_make_appeals = MagicMock(return_value=["Appeal with both citation types"])
        mock_sync_to_async.return_value = mock_make_appeals

        # Create the parameters dictionary
        parameters = {
            "denial_id": str(self.mock_denial.denial_id),
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        # Mock the behavior of other components to isolate the test
        with patch(
            "fighthealthinsurance.common_view_logic.Denial.objects.filter"
        ) as mock_filter:
            mock_queryset = AsyncMock()
            mock_queryset.select_related = MagicMock(return_value=mock_queryset)
            mock_queryset.aget.return_value = self.mock_denial
            mock_filter.return_value = mock_queryset

            # Mock the rest of the appeals generation process
            with patch("fighthealthinsurance.common_view_logic.a") as mock_a:
                mock_a.map.return_value = AsyncMock()

                # Create a generator function to yield from
                async def mock_yield():
                    yield "Appeal JSON data with both citation types"

                # Set up interleave_iterator to return our mock generator
                with patch(
                    "fighthealthinsurance.common_view_logic.interleave_iterator_for_keep_alive",
                    return_value=mock_yield(),
                ):
                    # Execute
                    result_generator = AppealsBackendHelper.generate_appeals(parameters)
                    result = [r async for r in result_generator]

                    # Verify the ML citations helper was called
                    mock_generate_citations.assert_awaited_once_with(
                        self.mock_denial, speculative=False
                    )

                    # Verify PubMed context was fetched
                    mock_find_pubmed_context.assert_awaited_once_with(self.mock_denial)

                    # Verify make_appeals was called with both contexts
                    mock_make_appeals.assert_called_once()
                    call_args = mock_make_appeals.call_args
                    # Check that both pubmed_context and ml_citations_context were included
                    self.assertIn("pubmed_context", call_args[1])
                    self.assertIn("ml_citations_context", call_args[1])
                    self.assertEqual(
                        call_args[1]["pubmed_context"], "PubMed context data"
                    )
                    self.assertEqual(
                        call_args[1]["ml_citations_context"],
                        ["ML Citation 1", "ML Citation 2"],
                    )

from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import asyncio

from fighthealthinsurance.common_view_logic import AppealsBackendHelper


async def passthrough_interleave(iterator):
    async for item in iterator:
        yield item


def _make_mock_denial():
    """Create a fully populated mock denial with all attributes generate_appeals needs."""
    denial = MagicMock()
    denial.denial_id = 12345
    denial.semi_sekret = "test-secret"
    denial.denial_text = "Test denial text"
    denial.procedure = "Test procedure"
    denial.diagnosis = "Test diagnosis"
    denial.health_history = "Test health history"
    denial.insurance_company = "Test Insurance"
    denial.claim_id = "CLAIM123"
    denial.date = "2025-01-01"
    denial.ml_citation_context = None
    denial.pubmed_context = None
    denial.qa_context = None
    denial.plan_context = None
    denial.plan_documents_summary = None
    denial.professional_to_finish = False
    denial.gen_attempts = 0
    denial.microsite_slug = None
    denial.patient_user = None
    denial.primary_professional = None
    denial.domain = None

    # asave and arefresh_from_db must be async mocks
    denial.asave = AsyncMock()
    denial.arefresh_from_db = AsyncMock()

    # denial_type.all() must return an empty async iterator
    async def empty_async_iter():
        return
        yield  # makes it an async generator

    denial.denial_type = MagicMock()
    denial.denial_type.all.return_value = empty_async_iter()

    return denial


def _make_mock_denial_query(denial):
    """Create a mock for Denial.objects.filter(...).select_related(...).aget()."""
    mock_queryset = MagicMock()
    mock_queryset.select_related.return_value = mock_queryset
    mock_queryset.aget = AsyncMock(return_value=denial)
    return mock_queryset


def _make_empty_proposed_appeal_query():
    """Create a mock for ProposedAppeal.objects.filter(for_denial=denial).all()."""
    async def empty_async_iter():
        return
        yield

    mock_queryset = MagicMock()
    mock_queryset.all.return_value = empty_async_iter()
    return mock_queryset


class TestAppealsBackendHelperWithCitations:
    """Tests for the AppealsBackendHelper class with ML citations integration.

    These test the citation-gathering portion of generate_appeals by mocking
    all dependencies (DB, appeal generator, pubmed, ML citations, etc.)
    """

    @pytest.mark.asyncio
    async def test_generate_appeals_with_ml_citations(self):
        """Test that ML citations are gathered and passed to make_appeals."""
        mock_denial = _make_mock_denial()
        mock_denial_query = _make_mock_denial_query(mock_denial)

        parameters = {
            "denial_id": "12345",
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        with (
            patch(
                "fighthealthinsurance.common_view_logic.Denial.objects.filter",
                return_value=mock_denial_query,
            ),
            patch(
                "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
                return_value="hashed",
            ),
            patch.object(
                AppealsBackendHelper,
                "regex_denial_processor",
            ) as mock_regex_processor,
            patch(
                "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
                new_callable=AsyncMock,
                return_value="ML Citation 1",
            ) as mock_generate_citations,
            patch.object(
                AppealsBackendHelper.pmt,
                "find_context_for_denial",
                new_callable=AsyncMock,
                return_value="PubMed context data",
            ),
            patch(
                "fighthealthinsurance.common_view_logic.sync_to_async",
            ) as mock_sync_to_async,
            patch(
                "fighthealthinsurance.common_view_logic.interleave_iterator_for_keep_alive",
            ) as mock_interleave,
            patch(
                "fighthealthinsurance.common_view_logic.ProposedAppeal",
            ) as mock_proposed_appeal_cls,
        ):
            # regex_denial_processor.get_appeal_templates returns empty
            mock_regex_processor.get_appeal_templates = AsyncMock(return_value=[])

            # sync_to_async(fn) returns an async callable; await result
            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Appeal text with citations"]
            )
            mock_sync_to_async.return_value = mock_make_appeals_wrapper

            # ProposedAppeal() save mock
            mock_pa_instance = MagicMock()
            mock_pa_instance.id = 1
            mock_pa_instance.asave = AsyncMock()
            mock_proposed_appeal_cls.return_value = mock_pa_instance

            # Configure ProposedAppeal.objects.filter to return empty async iterator
            mock_proposed_appeal_cls.objects.filter.return_value = (
                _make_empty_proposed_appeal_query()
            )

            mock_interleave.side_effect = passthrough_interleave

            # Collect all output from the generator
            result = []
            async for chunk in AppealsBackendHelper.generate_appeals(parameters):
                result.append(chunk)

            # Verify ML citations helper was called
            mock_generate_citations.assert_awaited_once_with(
                mock_denial, speculative=False
            )

            # Verify make_appeals was called with ml_citations_context
            mock_make_appeals_wrapper.assert_called_once()
            call_kwargs = mock_make_appeals_wrapper.call_args[1]
            assert call_kwargs["ml_citations_context"] == "ML Citation 1"

    @pytest.mark.asyncio
    async def test_pubmed_and_ml_citations_timeouts(self):
        """Test that timeouts in citation gathering don't crash generate_appeals."""
        mock_denial = _make_mock_denial()
        mock_denial_query = _make_mock_denial_query(mock_denial)

        parameters = {
            "denial_id": "12345",
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        with (
            patch(
                "fighthealthinsurance.common_view_logic.Denial.objects.filter",
                return_value=mock_denial_query,
            ),
            patch(
                "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
                return_value="hashed",
            ),
            patch.object(
                AppealsBackendHelper,
                "regex_denial_processor",
            ) as mock_regex_processor,
            patch(
                "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
                new_callable=AsyncMock,
                side_effect=asyncio.TimeoutError("ML citations timeout"),
            ),
            patch.object(
                AppealsBackendHelper.pmt,
                "find_context_for_denial",
                new_callable=AsyncMock,
                side_effect=asyncio.TimeoutError("PubMed timeout"),
            ),
            patch(
                "fighthealthinsurance.common_view_logic.sync_to_async",
            ) as mock_sync_to_async,
            patch(
                "fighthealthinsurance.common_view_logic.interleave_iterator_for_keep_alive",
            ) as mock_interleave,
            patch(
                "fighthealthinsurance.common_view_logic.ProposedAppeal",
            ) as mock_proposed_appeal_cls,
        ):
            mock_regex_processor.get_appeal_templates = AsyncMock(return_value=[])

            # sync_to_async(fn) returns an async callable; await result
            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Appeal text without citations"]
            )
            mock_sync_to_async.return_value = mock_make_appeals_wrapper

            mock_pa_instance = MagicMock()
            mock_pa_instance.id = 1
            mock_pa_instance.asave = AsyncMock()
            mock_proposed_appeal_cls.return_value = mock_pa_instance

            # Configure ProposedAppeal.objects.filter to return empty async iterator
            mock_proposed_appeal_cls.objects.filter.return_value = (
                _make_empty_proposed_appeal_query()
            )

            mock_interleave.side_effect = passthrough_interleave

            # Should NOT raise despite timeouts - the function handles them gracefully
            result = []
            async for chunk in AppealsBackendHelper.generate_appeals(parameters):
                result.append(chunk)

            # Verify we still got output (status messages + appeals)
            assert len(result) > 0

            # Verify make_appeals was still called (appeal gen continues despite timeout)
            mock_make_appeals_wrapper.assert_called_once()

            # Verify contexts are None since both timed out
            # asyncio.gather with return_exceptions=True returns the exceptions
            # which are not str instances, so both contexts should be None
            call_kwargs = mock_make_appeals_wrapper.call_args[1]
            assert call_kwargs.get("pubmed_context") is None
            assert call_kwargs.get("ml_citations_context") is None

    @pytest.mark.asyncio
    async def test_both_citation_sources_used(self):
        """Test that both PubMed and ML citations are passed to make_appeals."""
        mock_denial = _make_mock_denial()
        mock_denial_query = _make_mock_denial_query(mock_denial)

        parameters = {
            "denial_id": "12345",
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        with (
            patch(
                "fighthealthinsurance.common_view_logic.Denial.objects.filter",
                return_value=mock_denial_query,
            ),
            patch(
                "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
                return_value="hashed",
            ),
            patch.object(
                AppealsBackendHelper,
                "regex_denial_processor",
            ) as mock_regex_processor,
            patch(
                "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
                new_callable=AsyncMock,
                return_value="ML Citation 1",
            ) as mock_generate_citations,
            patch.object(
                AppealsBackendHelper.pmt,
                "find_context_for_denial",
                new_callable=AsyncMock,
                return_value="PubMed context data",
            ) as mock_find_pubmed,
            patch(
                "fighthealthinsurance.common_view_logic.sync_to_async",
            ) as mock_sync_to_async,
            patch(
                "fighthealthinsurance.common_view_logic.interleave_iterator_for_keep_alive",
            ) as mock_interleave,
            patch(
                "fighthealthinsurance.common_view_logic.ProposedAppeal",
            ) as mock_proposed_appeal_cls,
        ):
            mock_regex_processor.get_appeal_templates = AsyncMock(return_value=[])

            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Appeal with both citation types"]
            )
            mock_sync_to_async.return_value = mock_make_appeals_wrapper

            mock_pa_instance = MagicMock()
            mock_pa_instance.id = 1
            mock_pa_instance.asave = AsyncMock()
            mock_proposed_appeal_cls.return_value = mock_pa_instance

            # Configure ProposedAppeal.objects.filter to return empty async iterator
            mock_proposed_appeal_cls.objects.filter.return_value = (
                _make_empty_proposed_appeal_query()
            )

            mock_interleave.side_effect = passthrough_interleave

            result = []
            async for chunk in AppealsBackendHelper.generate_appeals(parameters):
                result.append(chunk)

            # Verify both citation sources were called
            mock_generate_citations.assert_awaited_once_with(
                mock_denial, speculative=False
            )
            mock_find_pubmed.assert_awaited_once_with(mock_denial)

            # Verify make_appeals received both citation contexts
            mock_make_appeals_wrapper.assert_called_once()
            call_kwargs = mock_make_appeals_wrapper.call_args[1]
            assert call_kwargs["pubmed_context"] == "PubMed context data"
            assert call_kwargs["ml_citations_context"] == "ML Citation 1"

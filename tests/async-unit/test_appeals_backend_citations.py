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


def _make_proposed_appeal_query_with_texts(texts):
    """Create a queryset-like object that yields ProposedAppeal-like mocks
    each carrying one of the given appeal texts. Supports both:
      - `qs.all()` (used at common_view_logic.py:~2410)
      - `async for pa in qs` (used in the synthesis branch)
    """
    pa_mocks = []
    for text in texts:
        pa = MagicMock()
        pa.appeal_text = text
        pa_mocks.append(pa)

    class _Queryset:
        def __init__(self, items):
            self._items = items

        def all(self):
            async def gen():
                for item in self._items:
                    yield item

            return gen()

        def __aiter__(self):
            async def gen():
                for item in self._items:
                    yield item

            return gen()

    return _Queryset(pa_mocks)


def _sync_to_async_router(pa_wrapper, make_appeals_wrapper, uspstf_wrapper=None):
    """
    Build a side_effect for the patched ``sync_to_async`` so the PA-context,
    USPSTF-context, and make_appeals calls each route to their own AsyncMock.
    Unexpected call sites raise so a future production change can't quietly
    funnel its sync_to_async target through the make_appeals mock.

    ``uspstf_wrapper`` defaults to an AsyncMock returning ``""`` (no
    matching preventive recommendations) so tests that don't care about
    the USPSTF integration don't have to wire it up.
    """
    if uspstf_wrapper is None:
        uspstf_wrapper = AsyncMock(return_value="")

    def route(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "") or repr(fn)
        if "get_pa_context_for_denial" in name:
            return pa_wrapper
        if "get_uspstf_context_for_denial" in name:
            return uspstf_wrapper
        if "make_appeals" in name:
            return make_appeals_wrapper
        raise AssertionError(
            f"Unexpected sync_to_async target in test: {name!r}. "
            "Update _sync_to_async_router to route this callable explicitly."
        )

    return route


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

            # sync_to_async(fn) is called twice: once to wrap the PA-context
            # lookup (sync ORM) and once to wrap make_appeals. Route each to
            # its own AsyncMock so we can assert make_appeals was invoked.
            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Appeal text with citations"]
            )
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper
            )

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

            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Appeal text without citations"]
            )
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper
            )

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
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper
            )

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

    @pytest.mark.asyncio
    async def test_uspstf_context_passed_to_make_appeals(self):
        """USPSTF context from the gather block is threaded into make_appeals.

        Mirrors the PA-context routing pattern: a stub
        ``get_uspstf_context_for_denial`` returns a known string and we
        verify it shows up as the ``uspstf_context`` kwarg on make_appeals.
        """
        mock_denial = _make_mock_denial()
        mock_denial_query = _make_mock_denial_query(mock_denial)

        parameters = {
            "denial_id": "12345",
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        uspstf_payload = (
            "USPSTF preventive-service recommendations relevant to this denial."
        )

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
            ),
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
            mock_regex_processor.get_appeal_templates = AsyncMock(return_value=[])

            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Appeal with USPSTF context"]
            )
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_uspstf_wrapper = AsyncMock(return_value=uspstf_payload)
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper, mock_uspstf_wrapper
            )

            mock_pa_instance = MagicMock()
            mock_pa_instance.id = 1
            mock_pa_instance.asave = AsyncMock()
            mock_proposed_appeal_cls.return_value = mock_pa_instance

            mock_proposed_appeal_cls.objects.filter.return_value = (
                _make_empty_proposed_appeal_query()
            )

            mock_interleave.side_effect = passthrough_interleave

            async for _ in AppealsBackendHelper.generate_appeals(parameters):
                pass

            mock_make_appeals_wrapper.assert_called_once()
            call_kwargs = mock_make_appeals_wrapper.call_args[1]
            assert call_kwargs["uspstf_context"] == uspstf_payload


class TestSynthesisThreshold:
    """Step 5 regression: synthesis must trigger when there is >=1 saved
    appeal (not >=2). When primary yields exactly one appeal, synthesis
    acts as a polish/expand pass that produces a second deliverable —
    previously this case skipped synthesis entirely."""

    @pytest.mark.asyncio
    async def test_synthesis_runs_with_single_saved_appeal(self):
        """When ProposedAppeal has exactly 1 entry, synthesize_appeals
        must be invoked (was previously gated at >=2)."""
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
                return_value="",
            ),
            patch.object(
                AppealsBackendHelper.pmt,
                "find_context_for_denial",
                new_callable=AsyncMock,
                return_value="",
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
            patch(
                "fighthealthinsurance.common_view_logic.appealGenerator"
            ) as mock_appeal_gen,
        ):
            mock_regex_processor.get_appeal_templates = AsyncMock(return_value=[])

            mock_make_appeals_wrapper = AsyncMock(
                return_value=["Saved appeal text long enough to be real"]
            )
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper
            )

            mock_pa_instance = MagicMock()
            mock_pa_instance.id = 1
            mock_pa_instance.asave = AsyncMock()
            mock_proposed_appeal_cls.return_value = mock_pa_instance

            # KEY: filter returns exactly one ProposedAppeal-like item
            mock_proposed_appeal_cls.objects.filter.return_value = (
                _make_proposed_appeal_query_with_texts(["single saved appeal text"])
            )

            mock_interleave.side_effect = passthrough_interleave

            # synthesize_appeals is the method we want to verify is called
            mock_appeal_gen.synthesize_appeals = AsyncMock(
                return_value="Synthesized appeal output"
            )

            async for _ in AppealsBackendHelper.generate_appeals(parameters):
                pass

            # The critical assertion: synthesize_appeals was invoked
            # because saved_appeal_texts had >= 1 entry.
            mock_appeal_gen.synthesize_appeals.assert_called_once()
            call_kwargs = mock_appeal_gen.synthesize_appeals.call_args[1]
            assert call_kwargs["appeal_texts"] == ["single saved appeal text"]

    @pytest.mark.asyncio
    async def test_synthesis_does_not_run_with_zero_saved_appeals(self):
        """When ProposedAppeal has 0 entries, synthesize_appeals must NOT
        be invoked (sanity check that the lower bound is still respected)."""
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
                return_value="",
            ),
            patch.object(
                AppealsBackendHelper.pmt,
                "find_context_for_denial",
                new_callable=AsyncMock,
                return_value="",
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
            patch(
                "fighthealthinsurance.common_view_logic.appealGenerator"
            ) as mock_appeal_gen,
        ):
            mock_regex_processor.get_appeal_templates = AsyncMock(return_value=[])

            mock_make_appeals_wrapper = AsyncMock(return_value=[])
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper
            )

            mock_pa_instance = MagicMock()
            mock_pa_instance.id = 1
            mock_pa_instance.asave = AsyncMock()
            mock_proposed_appeal_cls.return_value = mock_pa_instance

            # filter returns no items → saved_appeal_texts is empty
            mock_proposed_appeal_cls.objects.filter.return_value = (
                _make_proposed_appeal_query_with_texts([])
            )

            mock_interleave.side_effect = passthrough_interleave

            mock_appeal_gen.synthesize_appeals = AsyncMock(
                return_value="Should not be reached"
            )

            async for _ in AppealsBackendHelper.generate_appeals(parameters):
                pass

            mock_appeal_gen.synthesize_appeals.assert_not_called()

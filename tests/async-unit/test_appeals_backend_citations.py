from unittest.mock import patch, AsyncMock, MagicMock
import asyncio
import io
import json
from contextlib import contextmanager

import pytest
from loguru import logger as loguru_logger

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.generate_appeal import GeneratedAppeal


@contextmanager
def _loguru_capture(level="WARNING"):
    """Capture loguru records at or above ``level`` into a StringIO sink."""
    sink = io.StringIO()
    handler_id = loguru_logger.add(sink, level=level)
    try:
        yield sink
    finally:
        loguru_logger.remove(handler_id)


def _ga(text: str, model_name: str = "test-model"):
    """Shorthand for the appeal-generation iterator's GeneratedAppeal items."""
    return GeneratedAppeal(text=text, model_name=model_name)


def _collect_appeal_contents(chunks):
    """Extract the ``content`` field from every appeal JSON chunk."""
    contents = []
    for c in chunks:
        stripped = c.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "content" in parsed:
            contents.append(parsed["content"])
    return contents


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
                return_value=[_ga("Appeal text with citations")]
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
                return_value=[_ga("Appeal text without citations")]
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
                return_value=[_ga("Appeal with both citation types")]
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
                return_value=[_ga("Appeal with USPSTF context")]
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

    @pytest.mark.asyncio
    async def test_clinical_trials_context_passed_to_make_appeals(self):
        """ClinicalTrials context from the gather block is threaded into
        make_appeals.

        Mirrors the USPSTF/PA routing pattern, but the trials reader is an
        async method on the shared ``clinical_trials`` helper (DB-only — the
        live registry call happened earlier in the prefetch), so it's patched
        directly rather than through the ``sync_to_async`` router.
        """
        mock_denial = _make_mock_denial()
        mock_denial_query = _make_mock_denial_query(mock_denial)

        parameters = {
            "denial_id": "12345",
            "email": "test@example.com",
            "semi_sekret": "test-secret",
        }

        ct_payload = (
            "CLINICAL TRIAL EVIDENCE (relevant when the denial cites "
            '"experimental" or "investigational" grounds):\n\n'
            "NCT: NCT12345678; Title: Pembrolizumab in Melanoma"
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
            patch.object(
                AppealsBackendHelper.clinical_trials,
                "get_context_for_denial",
                new_callable=AsyncMock,
                return_value=ct_payload,
            ) as mock_get_ct_context,
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
                return_value=[_ga("Appeal with clinical trials context")]
            )
            mock_pa_wrapper = AsyncMock(return_value="")
            mock_sync_to_async.side_effect = _sync_to_async_router(
                mock_pa_wrapper, mock_make_appeals_wrapper
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

            # The reader was consulted (DB-only cache read), and its output
            # reached make_appeals as the clinical_trials_context kwarg.
            mock_get_ct_context.assert_awaited()
            mock_make_appeals_wrapper.assert_called_once()
            call_kwargs = mock_make_appeals_wrapper.call_args[1]
            assert call_kwargs["clinical_trials_context"] == ct_payload


@pytest.mark.parametrize(
    "saved_texts,synthesis_should_run",
    [
        # >=2 drafts: synthesis is meaningful (combines multiple inputs).
        (["draft one text body", "draft two text body"], True),
        # 1 draft: skipped — models tend to regurgitate a single input
        # verbatim, which the client then dedupes and reports as a
        # partial-delivery error.
        (["single saved appeal text"], False),
        ([], False),  # 0 saved appeals -> synthesis skipped
    ],
    ids=["two_saved_appeals", "one_saved_appeal", "zero_saved_appeals"],
)
@pytest.mark.asyncio
async def test_synthesis_threshold(saved_texts, synthesis_should_run):
    """Synthesis runs only when >=2 saved appeals exist."""
    mock_denial = _make_mock_denial()
    parameters = {
        "denial_id": "12345",
        "email": "test@example.com",
        "semi_sekret": "test-secret",
    }

    with (
        patch(
            "fighthealthinsurance.common_view_logic.Denial.objects.filter",
            return_value=_make_mock_denial_query(mock_denial),
        ),
        patch(
            "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
            return_value="hashed",
        ),
        patch.object(AppealsBackendHelper, "regex_denial_processor") as mock_regex,
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
            side_effect=passthrough_interleave,
        ),
        patch(
            "fighthealthinsurance.common_view_logic.ProposedAppeal",
        ) as mock_pa_cls,
        patch(
            "fighthealthinsurance.common_view_logic.appealGenerator"
        ) as mock_appeal_gen,
    ):
        mock_regex.get_appeal_templates = AsyncMock(return_value=[])
        mock_sync_to_async.side_effect = _sync_to_async_router(
            AsyncMock(return_value=""),  # pa_wrapper
            AsyncMock(
                return_value=[_ga(t) for t in saved_texts]
            ),  # make_appeals_wrapper
        )
        mock_pa_cls.return_value = MagicMock(id=1, asave=AsyncMock())
        mock_pa_cls.objects.filter.return_value = (
            _make_proposed_appeal_query_with_texts(saved_texts)
        )
        mock_appeal_gen.synthesize_appeals = AsyncMock(
            return_value="synthesized appeal letter text"
        )

        async for _ in AppealsBackendHelper.generate_appeals(parameters):
            pass

        if synthesis_should_run:
            mock_appeal_gen.synthesize_appeals.assert_called_once()
            assert (
                mock_appeal_gen.synthesize_appeals.call_args[1]["appeal_texts"]
                == saved_texts
            )
            # The synthesized appeal is persisted with synthesized=True so the
            # provenance is tracked independently of model_name.
            assert any(
                call.kwargs.get("synthesized") is True
                for call in mock_pa_cls.call_args_list
            ), "expected a ProposedAppeal saved with synthesized=True"
        else:
            mock_appeal_gen.synthesize_appeals.assert_not_called()


@pytest.mark.asyncio
async def test_synthesis_skips_verbatim_duplicate():
    """If the synthesizer returns text matching a saved draft, it must not be
    yielded — otherwise the client's content-based dedup silently drops it
    and reports a spurious "partial delivery" error.
    """
    mock_denial = _make_mock_denial()
    parameters = {
        "denial_id": "12345",
        "email": "test@example.com",
        "semi_sekret": "test-secret",
    }
    saved_texts = ["draft one text body", "draft two text body"]

    with (
        patch(
            "fighthealthinsurance.common_view_logic.Denial.objects.filter",
            return_value=_make_mock_denial_query(mock_denial),
        ),
        patch(
            "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
            return_value="hashed",
        ),
        patch.object(AppealsBackendHelper, "regex_denial_processor") as mock_regex,
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
            side_effect=passthrough_interleave,
        ),
        patch(
            "fighthealthinsurance.common_view_logic.ProposedAppeal",
        ) as mock_pa_cls,
        patch(
            "fighthealthinsurance.common_view_logic.appealGenerator"
        ) as mock_appeal_gen,
    ):
        mock_regex.get_appeal_templates = AsyncMock(return_value=[])
        mock_sync_to_async.side_effect = _sync_to_async_router(
            AsyncMock(return_value=""),
            AsyncMock(return_value=[_ga(t) for t in saved_texts]),
        )
        mock_pa_cls.return_value = MagicMock(id=1, asave=AsyncMock())
        mock_pa_cls.objects.filter.return_value = (
            _make_proposed_appeal_query_with_texts(saved_texts)
        )
        # Synthesizer returns a verbatim copy of the first saved draft —
        # the server must NOT yield it as a "new" appeal.
        mock_appeal_gen.synthesize_appeals = AsyncMock(return_value=saved_texts[0])

        chunks = []
        async for chunk in AppealsBackendHelper.generate_appeals(parameters):
            chunks.append(chunk)

    # The "done" frame should report only the streamed drafts, not the
    # deduped synthesis: new_appeals should equal len(saved_texts).
    done_frames = []
    for c in chunks:
        stripped = c.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("phase") == "done":
            done_frames.append(parsed)
    assert len(done_frames) == 1, f"expected exactly one done frame, got {done_frames}"
    assert done_frames[0]["new_appeals"] == len(saved_texts)
    # And no chunk should carry the "synthesized": "true" marker.
    for c in chunks:
        assert (
            '"synthesized": "true"' not in c
        ), f"verbatim-duplicate synthesis should have been skipped, got chunk: {c}"


@pytest.mark.asyncio
async def test_synthesis_too_short_output_is_filtered():
    """A synthesized appeal below MIN_APPEAL_CHARS must not be yielded — it
    would otherwise bypass the minimum-length rule the streaming path
    enforces — and the drop must be logged as a warning."""
    mock_denial = _make_mock_denial()
    parameters = {
        "denial_id": "12345",
        "email": "test@example.com",
        "semi_sekret": "test-secret",
    }
    saved_texts = ["draft one text body", "draft two text body"]

    with (
        patch(
            "fighthealthinsurance.common_view_logic.Denial.objects.filter",
            return_value=_make_mock_denial_query(mock_denial),
        ),
        patch(
            "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
            return_value="hashed",
        ),
        patch.object(AppealsBackendHelper, "regex_denial_processor") as mock_regex,
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
            side_effect=passthrough_interleave,
        ),
        patch(
            "fighthealthinsurance.common_view_logic.ProposedAppeal",
        ) as mock_pa_cls,
        patch(
            "fighthealthinsurance.common_view_logic.appealGenerator"
        ) as mock_appeal_gen,
    ):
        mock_regex.get_appeal_templates = AsyncMock(return_value=[])
        # make_appeals returns nothing new so the only synthesis candidate is
        # the (too-short) synthesizer output under test.
        mock_sync_to_async.side_effect = _sync_to_async_router(
            AsyncMock(return_value=""),
            AsyncMock(return_value=[]),
        )
        mock_pa_cls.return_value = MagicMock(id=1, asave=AsyncMock())
        mock_pa_cls.objects.filter.return_value = (
            _make_proposed_appeal_query_with_texts(saved_texts)
        )
        # Synthesizer returns a non-empty but too-short letter (< 15 chars).
        mock_appeal_gen.synthesize_appeals = AsyncMock(return_value="too short")

        with _loguru_capture() as sink:
            chunks = []
            async for chunk in AppealsBackendHelper.generate_appeals(parameters):
                chunks.append(chunk)

    # The too-short synthesis must never be delivered.
    contents = _collect_appeal_contents(chunks)
    assert "too short" not in contents
    for c in chunks:
        assert '"synthesized": "true"' not in c
    # And the drop is logged as a warning naming the denial.
    output = sink.getvalue()
    assert "too-short appeal" in output
    assert "synthesis output for denial 12345" in output


@pytest.mark.asyncio
async def test_existing_too_short_appeal_is_skipped():
    """Previously-saved appeals below MIN_APPEAL_CHARS (e.g. saved before the
    threshold existed) must not be re-delivered, and the skip is warned."""
    mock_denial = _make_mock_denial()
    parameters = {
        "denial_id": "12345",
        "email": "test@example.com",
        "semi_sekret": "test-secret",
    }
    # One runt (4 chars) and one deliverable existing appeal.
    existing_texts = ["tiny", "a valid existing appeal body"]

    with (
        patch(
            "fighthealthinsurance.common_view_logic.Denial.objects.filter",
            return_value=_make_mock_denial_query(mock_denial),
        ),
        patch(
            "fighthealthinsurance.common_view_logic.Denial.get_hashed_email",
            return_value="hashed",
        ),
        patch.object(AppealsBackendHelper, "regex_denial_processor") as mock_regex,
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
            side_effect=passthrough_interleave,
        ),
        patch(
            "fighthealthinsurance.common_view_logic.ProposedAppeal",
        ) as mock_pa_cls,
        patch(
            "fighthealthinsurance.common_view_logic.appealGenerator"
        ) as mock_appeal_gen,
    ):
        mock_regex.get_appeal_templates = AsyncMock(return_value=[])
        mock_sync_to_async.side_effect = _sync_to_async_router(
            AsyncMock(return_value=""),
            AsyncMock(return_value=[]),  # no newly generated appeals
        )
        mock_pa_cls.return_value = MagicMock(id=1, asave=AsyncMock())
        mock_pa_cls.objects.filter.return_value = (
            _make_proposed_appeal_query_with_texts(existing_texts)
        )
        mock_appeal_gen.synthesize_appeals = AsyncMock(return_value=None)

        with _loguru_capture() as sink:
            chunks = []
            async for chunk in AppealsBackendHelper.generate_appeals(parameters):
                chunks.append(chunk)

    contents = _collect_appeal_contents(chunks)
    # The runt is dropped; the valid existing appeal is delivered.
    assert "tiny" not in contents
    assert "a valid existing appeal body" in contents
    # The drop is logged as a warning identifying it as a saved appeal.
    output = sink.getvalue()
    assert "too-short appeal" in output
    assert "saved appeal id=" in output

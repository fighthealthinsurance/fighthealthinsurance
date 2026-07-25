"""Tests for MLAppealContextHelper.maybe_summarize_denial_text.

The helper condenses an oversized denial letter (cached) so it doesn't
overflow the model context window, while PREFERRING full context for
normal-sized denials and never routing PHI to an external provider for an
opt-out denial.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance.ml.ml_appeal_context_helper import MLAppealContextHelper

_HELPER = "fighthealthinsurance.ml.ml_appeal_context_helper"
# Comfortably above DENIAL_TEXT_SUMMARY_THRESHOLD_TOKENS (6000 tok ~= 24k chars).
_LONG_TEXT = "x" * 30000


def _denial(
    denial_text,
    summary=None,
    use_external=False,
    denial_id=7,
    candidate_summary=None,
):
    d = MagicMock()
    d.denial_text = denial_text
    d.denial_text_summary = summary
    # Explicit None (not an auto-MagicMock attr, which would read as truthy and
    # short-circuit the candidate-promotion path).
    d.candidate_denial_text_summary = candidate_summary
    d.use_external = use_external
    d.denial_id = denial_id
    return d


@pytest.mark.asyncio
async def test_below_threshold_returns_none_prefers_full_context():
    d = _denial("a short denial letter")
    with patch(f"{_HELPER}.ml_router") as router:
        router.summarize = AsyncMock(return_value="should not be called")
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result is None
    router.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_missing_denial_text_returns_none():
    d = _denial(None)
    with patch(f"{_HELPER}.ml_router") as router:
        router.summarize = AsyncMock()
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result is None
    router.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_cached_summary_short_circuits():
    d = _denial(_LONG_TEXT, summary="cached summary")
    with patch(f"{_HELPER}.ml_router") as router:
        router.summarize = AsyncMock(return_value="fresh summary")
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result == "cached summary"
    router.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_above_threshold_summarizes_and_persists():
    d = _denial(_LONG_TEXT, use_external=False, denial_id=7)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="CONDENSED")
        Denial.objects.filter.return_value.aupdate = AsyncMock()
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)

    assert result == "CONDENSED"
    # Privacy gate threaded through.
    assert router.summarize.call_args.kwargs["use_external"] is False
    # Cached back onto the denial row.
    Denial.objects.filter.assert_called_once_with(denial_id=7)
    Denial.objects.filter.return_value.aupdate.assert_awaited_once_with(
        denial_text_summary="CONDENSED"
    )


@pytest.mark.asyncio
async def test_use_external_true_is_passed_through():
    d = _denial(_LONG_TEXT, use_external=True)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="CONDENSED")
        Denial.objects.filter.return_value.aupdate = AsyncMock()
        await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert router.summarize.call_args.kwargs["use_external"] is True


@pytest.mark.asyncio
async def test_summarizer_returning_nothing_falls_back_to_full_text():
    d = _denial(_LONG_TEXT)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value=None)
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result is None
    # Nothing to cache when there is no summary.
    Denial.objects.filter.assert_not_called()


@pytest.mark.asyncio
async def test_summarizer_exception_falls_back_to_full_text():
    d = _denial(_LONG_TEXT)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(side_effect=RuntimeError("model down"))
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result is None
    Denial.objects.filter.assert_not_called()


@pytest.mark.asyncio
async def test_prewarmed_candidate_is_promoted_without_recomputing():
    """A pre-warmed candidate summary is promoted into the real field and
    returned, without calling the summarizer again."""
    d = _denial(_LONG_TEXT, candidate_summary="PREWARMED")
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="fresh summary")
        Denial.objects.filter.return_value.aupdate = AsyncMock()
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result == "PREWARMED"
    router.summarize.assert_not_called()
    # Promoted into the real field so later reads short-circuit.
    Denial.objects.filter.return_value.aupdate.assert_awaited_once_with(
        denial_text_summary="PREWARMED"
    )


@pytest.mark.asyncio
async def test_real_summary_wins_over_candidate():
    """When both a real summary and a candidate exist, the real one is used and
    the candidate is ignored (no promotion write)."""
    d = _denial(_LONG_TEXT, summary="REAL", candidate_summary="PREWARMED")
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock()
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result == "REAL"
    router.summarize.assert_not_called()
    Denial.objects.filter.assert_not_called()


@pytest.mark.asyncio
async def test_prewarm_writes_candidate_field_not_real():
    """The speculative pre-warm computes into candidate_denial_text_summary,
    never the real denial_text_summary."""
    d = _denial(_LONG_TEXT, use_external=False, denial_id=9)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="CANDIDATE")
        Denial.objects.filter.return_value.aupdate = AsyncMock()
        result = await MLAppealContextHelper.prewarm_candidate_denial_text_summary(d)
    assert result == "CANDIDATE"
    Denial.objects.filter.assert_called_once_with(denial_id=9)
    Denial.objects.filter.return_value.aupdate.assert_awaited_once_with(
        candidate_denial_text_summary="CANDIDATE"
    )


@pytest.mark.asyncio
async def test_prewarm_below_threshold_is_noop():
    """Normal-sized denials are never pre-warmed (full context preferred)."""
    d = _denial("a short denial letter")
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="should not be called")
        result = await MLAppealContextHelper.prewarm_candidate_denial_text_summary(d)
    assert result is None
    router.summarize.assert_not_called()
    Denial.objects.filter.assert_not_called()


@pytest.mark.asyncio
async def test_prewarm_skips_when_summary_already_exists():
    """Idempotent: if a real or candidate summary already exists, the pre-warm
    doesn't recompute."""
    d = _denial(_LONG_TEXT, candidate_summary="ALREADY")
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="fresh")
        result = await MLAppealContextHelper.prewarm_candidate_denial_text_summary(d)
    assert result == "ALREADY"
    router.summarize.assert_not_called()
    Denial.objects.filter.assert_not_called()

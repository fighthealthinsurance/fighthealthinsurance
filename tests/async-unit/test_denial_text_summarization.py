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
# Long enough to clear MIN_USABLE_SUMMARY_CHARS: the helper rejects anything
# too short to plausibly be a summary of a >24k-char letter.
_CONDENSED = (
    "Aetna denied a lumbar MRI (CPT 72148) for chronic low back pain, "
    "citing lack of documented conservative therapy under policy CPB-0009."
)


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
        router.summarize = AsyncMock(return_value=_CONDENSED)
        Denial.objects.filter.return_value.aupdate = AsyncMock(return_value=1)
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)

    assert result == _CONDENSED
    # Privacy gate threaded through.
    assert router.summarize.call_args.kwargs["use_external"] is False
    # Cached back onto the denial row, gated on the letter it summarizes.
    Denial.objects.filter.assert_called_once_with(denial_id=7, denial_text=_LONG_TEXT)
    Denial.objects.filter.return_value.aupdate.assert_awaited_once_with(
        denial_text_summary=_CONDENSED
    )


@pytest.mark.asyncio
async def test_use_external_true_is_passed_through():
    d = _denial(_LONG_TEXT, use_external=True)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value=_CONDENSED)
        Denial.objects.filter.return_value.aupdate = AsyncMock(return_value=1)
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


@pytest.mark.parametrize(
    "degenerate",
    ["", "   ", "\n\n", "bad!", "I can't help with that."],
)
@pytest.mark.asyncio
async def test_degenerate_summary_is_rejected_and_not_cached(degenerate):
    """A blank/trivial/refusal response must never be accepted: it REPLACES the
    user's denial letter in the prompt and gets cached, so every later
    generation would reuse it. Falling back to the full text is safe -- the shed
    ladder still handles overflow."""
    d = _denial(_LONG_TEXT)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value=degenerate)
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result is None
    # Nothing degenerate may reach the cache.
    Denial.objects.filter.assert_not_called()


@pytest.mark.asyncio
async def test_prewarmed_candidate_is_promoted_without_recomputing():
    """A pre-warmed candidate summary is promoted into the real field and
    returned, without calling the summarizer again."""
    d = _denial(_LONG_TEXT, candidate_summary="PREWARMED")
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value="fresh summary")
        Denial.objects.filter.return_value.aupdate = AsyncMock(return_value=1)
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)
    assert result == "PREWARMED"
    router.summarize.assert_not_called()
    # Promoted into the real field so later reads short-circuit, gated on the
    # letter the candidate was pre-warmed from.
    Denial.objects.filter.assert_called_once_with(denial_id=7, denial_text=_LONG_TEXT)
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
        router.summarize = AsyncMock(return_value=_CONDENSED)
        Denial.objects.filter.return_value.aupdate = AsyncMock(return_value=1)
        result = await MLAppealContextHelper.prewarm_candidate_denial_text_summary(d)
    assert result == _CONDENSED
    Denial.objects.filter.assert_called_once_with(denial_id=9, denial_text=_LONG_TEXT)
    Denial.objects.filter.return_value.aupdate.assert_awaited_once_with(
        candidate_denial_text_summary=_CONDENSED
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


@pytest.mark.asyncio
async def test_prewarm_cache_write_is_gated_on_the_letter_it_summarized():
    """The candidate write must not land if the letter was replaced mid-flight.

    The pre-warm runs in the background with no deadline, so a user correcting a
    bad OCR pass can replace the letter while it is still summarizing. The
    replacement's invalidation sweep has already run by then, so an
    unconditional write would leave a summary of the OLD letter behind it
    forever -- and the live flow substitutes that for the user's actual letter.
    Conditioning the UPDATE on denial_text means zero rows match and it drops.
    """
    d = _denial(_LONG_TEXT, use_external=False, denial_id=11)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value=_CONDENSED)
        # 0 rows updated == the row no longer has this denial_text.
        Denial.objects.filter.return_value.aupdate = AsyncMock(return_value=0)
        result = await MLAppealContextHelper.prewarm_candidate_denial_text_summary(d)

    # The in-flight caller still gets the summary (its own run is about the
    # same letter); only the persisted copy is dropped.
    assert result == _CONDENSED
    Denial.objects.filter.assert_called_once_with(denial_id=11, denial_text=_LONG_TEXT)


@pytest.mark.asyncio
async def test_live_summary_cache_write_is_gated_on_the_letter_it_summarized():
    """Same guard on the live path: a summary computed from a letter that has
    since been replaced must not be cached as that denial's summary."""
    d = _denial(_LONG_TEXT, use_external=False, denial_id=12)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value=_CONDENSED)
        Denial.objects.filter.return_value.aupdate = AsyncMock(return_value=0)
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)

    assert result == _CONDENSED
    Denial.objects.filter.assert_called_once_with(denial_id=12, denial_text=_LONG_TEXT)


@pytest.mark.asyncio
async def test_cache_write_failure_still_returns_the_summary():
    """A DB failure caching the summary is non-fatal: the generation already
    under way still gets its condensed text."""
    d = _denial(_LONG_TEXT, use_external=False, denial_id=13)
    with patch(f"{_HELPER}.ml_router") as router, patch(f"{_HELPER}.Denial") as Denial:
        router.summarize = AsyncMock(return_value=_CONDENSED)
        Denial.objects.filter.return_value.aupdate = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        result = await MLAppealContextHelper.maybe_summarize_denial_text(d)

    assert result == _CONDENSED

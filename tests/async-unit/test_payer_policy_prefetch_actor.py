"""Unit tests for PayerPolicyPrefetchActor's testable helpers (without Ray)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance.payer_policy_prefetch_actor import PayerPolicyPrefetchActor

# Access the underlying class through the Ray decorator's __ray_metadata__.
# When @ray.remote wraps a class, the original class is accessible at this
# attribute; falling back to the decorated object if Ray isn't loaded.
_Klass = getattr(PayerPolicyPrefetchActor, "__ray_metadata__", None)
_Underlying = _Klass.modified_class if _Klass else PayerPolicyPrefetchActor


def _make_actor() -> "_Underlying":  # type: ignore[valid-type]
    """Build an actor instance without running __init__ (which boots Django)."""
    actor = _Underlying.__new__(_Underlying)
    actor.fetched = 0
    actor.failed = 0
    actor.entries = 0
    return actor


class TestPrefetchAll:
    @pytest.mark.asyncio
    async def test_prefetch_all_records_stats_from_fetcher(self):
        actor = _make_actor()

        fake_fetcher = MagicMock()
        fake_fetcher.__aenter__ = AsyncMock(return_value=fake_fetcher)
        fake_fetcher.__aexit__ = AsyncMock(return_value=None)
        fake_fetcher.ingest_all = AsyncMock(
            return_value={"fetched": 2, "failed": 1, "entries": 17}
        )

        with patch(
            "fighthealthinsurance.payer_policy_fetcher.PayerPolicyFetcher",
            return_value=fake_fetcher,
        ):
            result = await actor.prefetch_all()

        assert result["fetched"] == 2
        assert result["failed"] == 1
        assert result["entries"] == 17
        assert "duration_seconds" in result
        assert actor.fetched == 2
        assert actor.failed == 1
        assert actor.entries == 17
        fake_fetcher.ingest_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prefetch_all_surfaces_errors_without_raising(self):
        actor = _make_actor()

        fake_fetcher = MagicMock()
        fake_fetcher.__aenter__ = AsyncMock(return_value=fake_fetcher)
        fake_fetcher.__aexit__ = AsyncMock(return_value=None)
        fake_fetcher.ingest_all = AsyncMock(side_effect=RuntimeError("upstream down"))

        with patch(
            "fighthealthinsurance.payer_policy_fetcher.PayerPolicyFetcher",
            return_value=fake_fetcher,
        ):
            result = await actor.prefetch_all()

        # Errors must not propagate -- the deploy-time job should keep going.
        assert "error" in result
        assert "upstream down" in result["error"]
        # An ingest_all failure counts as one bumped failure on top of the
        # stats we managed to capture (none, since the call raised).
        assert result["failed"] == 1


class TestGetStats:
    @pytest.mark.asyncio
    async def test_get_stats_returns_current_counters(self):
        actor = _make_actor()
        actor.fetched = 5
        actor.failed = 2
        actor.entries = 42

        stats = await actor.get_stats()
        assert stats == {"fetched": 5, "failed": 2, "entries": 42}

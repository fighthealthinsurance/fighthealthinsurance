"""The conftest retries a failed Temporal server download, and only that."""

from unittest.mock import AsyncMock, patch

import pytest

from tests import temporal_download_retry as conftest

_RATE_LIMITED = RuntimeError(
    "Failed starting test server: failed to download ephemeral server "
    "executable: HTTP status client error (429 Too Many Requests)"
)


@pytest.fixture(autouse=True)
def _no_backoff_sleep():
    with patch.object(conftest.asyncio, "sleep", AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_a_rate_limited_download_is_retried_until_it_succeeds():
    start = AsyncMock(side_effect=[_RATE_LIMITED, _RATE_LIMITED, "env"])
    assert await conftest._retrying_download(start)() == "env"


@pytest.mark.asyncio
async def test_a_download_that_keeps_failing_is_raised_after_the_last_retry():
    start = AsyncMock(side_effect=_RATE_LIMITED)
    with pytest.raises(RuntimeError, match="failed to download"):
        await conftest._retrying_download(start)()
    assert start.await_count == len(conftest._DOWNLOAD_RETRY_DELAYS) + 1


@pytest.mark.asyncio
async def test_any_other_failure_is_raised_at_once():
    start = AsyncMock(side_effect=RuntimeError("port in use"))
    with pytest.raises(RuntimeError, match="port in use"):
        await conftest._retrying_download(start)()
    assert start.await_count == 1


def test_the_environment_starters_are_wrapped():
    from temporalio.testing import WorkflowEnvironment

    for name in ("start_local", "start_time_skipping"):
        assert getattr(WorkflowEnvironment, name).__wrapped__ is not None

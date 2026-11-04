"""Retry a Temporal test-server download that failed (see tests/temporal/conftest.py)."""

import asyncio
import functools

# 2 + 4 + 8 + 16 seconds: a rate limit gets half a minute to clear.
_DOWNLOAD_RETRY_DELAYS = (2, 4, 8, 16)


def _is_download_failure(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and "failed to download" in str(error)


def _retrying_download(start):
    @functools.wraps(start)
    async def wrapper(*args, **kwargs):
        for delay in _DOWNLOAD_RETRY_DELAYS:
            try:
                return await start(*args, **kwargs)
            except RuntimeError as e:
                if not _is_download_failure(e):
                    raise
            await asyncio.sleep(delay)
        return await start(*args, **kwargs)

    return wrapper

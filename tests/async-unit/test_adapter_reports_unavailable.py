"""The adapter tells an entity reader when the provider could not be reached.

Chat and appeal generation read None as "try the next model". Entity
extraction asks for the difference between a model that answered nothing
and a model that was never reached, and gets it at every boundary that
used to swallow it: the transport, the outer per-call deadline, and the
rate-limited wrapper's own skips. The transport is mocked at the aiohttp
boundary: nothing here opens a socket.
"""

import asyncio
from unittest.mock import patch

import aiohttp
import pytest

from fighthealthinsurance.ml.ml_models import (
    ProviderUnavailable,
    RemoteFullOpenLike,
    RemoteOpenLike,
)


def _adapter():
    return RemoteFullOpenLike(
        api_base="http://provider.invalid/v1", token="not-a-token", model="not-a-model"
    )


def _refused(*args, **kwargs):
    raise aiohttp.ClientConnectionError("connection refused")


@pytest.mark.asyncio
async def test_an_outer_deadline_expiring_is_reported_as_unreachable():
    async def never_answers(*args, **kwargs):
        await asyncio.sleep(5)

    with patch.object(
        RemoteOpenLike, "_RemoteOpenLike__infer", new=never_answers
    ), patch("fighthealthinsurance.ml.ml_models.ml_task_timeout", return_value=0.05):
        with pytest.raises(ProviderUnavailable):
            await _adapter().get_plan_id("a letter")


@pytest.mark.asyncio
async def test_a_refused_connection_is_reported_as_unreachable():
    with patch.object(aiohttp.ClientSession, "post", side_effect=_refused):
        with pytest.raises(ProviderUnavailable):
            await _adapter().get_plan_id("a letter")


@pytest.mark.asyncio
async def test_without_asking_the_adapter_still_answers_none():
    """The default contract for every other caller is unchanged."""
    with patch.object(aiohttp.ClientSession, "post", side_effect=_refused):
        result = await _adapter()._infer_no_context(
            system_prompts=["x"], prompt="a letter", timeout=1
        )
    assert result is None

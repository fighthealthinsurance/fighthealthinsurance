"""The fax polling and UCR refresh actors' run guards, without Ray.

A fresh process attaching to a running actor calls run() again (see
BaseActorRef.get). These are async actors, so the second call ran
concurrently with the first as a second loop, one nothing could see or stop.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from fighthealthinsurance.base_actor_ref import RUN_ALREADY_STARTED
from fighthealthinsurance.fax_polling_actor import FaxPollingActor
from fighthealthinsurance.ucr_refresh_actor import (
    UCRRefreshActor,
    UCRRefreshController,
)


def _underlying(actor_class):
    """The class under the Ray decorator (see test_imr_refresh_actor.py)."""
    klass = getattr(actor_class, "__ray_metadata__", None)
    return klass.modified_class if klass else actor_class


def _running_fax_actor():
    klass = _underlying(FaxPollingActor)
    # __new__ bypasses __init__, which creates the FaxActor it polls.
    actor = klass.__new__(klass)
    actor._logger = MagicMock()
    actor.running = True
    actor.fax_actor = MagicMock()
    actor.fax_actor.send_delayed_faxes.remote = AsyncMock(
        side_effect=AssertionError("a second loop polled")
    )
    return actor


def _running_ucr_actor():
    klass = _underlying(UCRRefreshActor)
    # __new__ bypasses __init__ (Django bootstrap plus a sleep).
    actor = klass.__new__(klass)
    controller = UCRRefreshController(MagicMock())
    controller.running = True
    controller.source_refresh_loop = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("a second loop ran")
    )
    controller.denial_refresh_loop = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("a second loop ran")
    )
    actor._controller = controller
    return actor


@pytest.mark.asyncio
async def test_a_second_fax_polling_run_returns_at_once():
    assert await _running_fax_actor().run() == RUN_ALREADY_STARTED


@pytest.mark.asyncio
async def test_a_second_ucr_refresh_run_returns_at_once():
    assert await _running_ucr_actor().run() == RUN_ALREADY_STARTED


@pytest.mark.asyncio
async def test_a_second_ucr_refresh_run_leaves_the_first_marked_running():
    """run()'s finally marks the loops stopped, so a second run that got as
    far as the loops would also have made the health check report the live
    first pair as down."""
    actor = _running_ucr_actor()
    await actor.run()
    assert await actor.health_check() is True

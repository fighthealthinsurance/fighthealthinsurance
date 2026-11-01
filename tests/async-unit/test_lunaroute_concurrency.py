"""LunaRoute takes one call at a time per model and hard-fails the rest, so
a second call while one is in flight in this process is never made: it
fails at once, as a backend backing off after a 429 does, and the caller
moves on to the next backend. The transport (RemoteFullOpenLike._infer, the
super of LunaRoute's) is mocked: nothing here opens a socket.
"""

import asyncio
from unittest.mock import patch

import pytest

from fighthealthinsurance.ml import ml_models
from fighthealthinsurance.ml.ml_models import (
    LocalConcurrencyGate,
    LunaRoute,
    ProviderUnavailable,
    RemoteFullOpenLike,
    max_parallel_from_env,
)


@pytest.fixture
def env():
    """The environment LunaRoute reads, never the real one: a fake key, and
    whatever a test adds."""
    values = {"LUNA_ROUTE_KEY": "lr_test"}
    with patch.object(ml_models, "get_env_variable", side_effect=values.get):
        yield values


@pytest.fixture
def gate():
    """A fresh gate, so no test sees another's slots."""
    fresh = LocalConcurrencyGate()
    with patch.object(LunaRoute, "_concurrency", fresh):
        yield fresh


@pytest.fixture
def held_transport():
    """A transport whose calls wait until ``release`` is set, counting how
    many were made."""
    release = asyncio.Event()
    calls = []

    async def transport(self, *args, **kwargs):
        calls.append(self.model)
        await release.wait()
        return ("a letter", None)

    with patch.object(RemoteFullOpenLike, "_infer", new=transport):
        yield release, calls


async def _ask(model, **kwargs):
    return await model._infer(system_prompts=["s"], prompt="p", **kwargs)


async def _start_first_call(model):
    """Start a call that holds the model's slot; returns its task."""
    task = asyncio.create_task(_ask(model))
    for _ in range(10):
        await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_second_call_while_one_is_in_flight_returns_none_without_calling(
    env, gate, held_transport
):
    release, calls = held_transport
    model = LunaRoute(model="glm-5.3")
    first = await _start_first_call(model)

    second = await asyncio.wait_for(_ask(model), timeout=1)

    release.set()
    assert (second, await first, calls) == (None, ("a letter", None), ["glm-5.3"])


@pytest.mark.asyncio
async def test_busy_call_raises_provider_unavailable_when_asked_to(
    env, gate, held_transport
):
    release, _calls = held_transport
    model = LunaRoute(model="glm-5.3")
    first = await _start_first_call(model)
    try:
        with pytest.raises(ProviderUnavailable):
            await asyncio.wait_for(_ask(model, raise_on_unavailable=True), timeout=1)
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_busy_call_is_counted_as_skipped_busy(env, gate, held_transport):
    release, _calls = held_transport
    model = LunaRoute(model="glm-5.3")
    first = await _start_first_call(model)
    with patch.object(ml_models, "record_ml_failure") as failure:
        await _ask(model)
    release.set()
    await first
    assert failure.call_args.args[1] == "skipped_busy"


@pytest.mark.asyncio
async def test_slot_is_free_again_once_the_call_ends(env, gate, held_transport):
    release, calls = held_transport
    release.set()
    model = LunaRoute(model="glm-5.3")
    await _ask(model)
    assert (await _ask(model), len(calls)) == (("a letter", None), 2)


@pytest.mark.asyncio
async def test_slot_is_freed_when_the_call_raises(env, gate):
    async def broken(self, *args, **kwargs):
        raise RuntimeError("boom")

    model = LunaRoute(model="glm-5.3")
    with patch.object(RemoteFullOpenLike, "_infer", new=broken):
        with pytest.raises(RuntimeError):
            await _ask(model)
    assert gate.in_flight((model.api_base, model.model)) == 0


@pytest.mark.asyncio
async def test_the_other_model_is_not_held_back(env, gate, held_transport):
    """The limit is per model: the arena asks both LunaRoute models at once."""
    release, calls = held_transport
    glm = LunaRoute(model="glm-5.3")
    deepseek = LunaRoute(model="deepseek-4.1-flash")
    first = await _start_first_call(glm)
    second = asyncio.create_task(_ask(deepseek))
    for _ in range(10):
        await asyncio.sleep(0)
    release.set()
    await first
    assert (await second, sorted(calls)) == (
        ("a letter", None),
        ["deepseek-4.1-flash", "glm-5.3"],
    )


@pytest.mark.asyncio
async def test_a_raised_limit_lets_two_calls_through(env, gate, held_transport):
    env["LUNA_ROUTE_MAX_PARALLEL"] = "2"
    release, calls = held_transport
    model = LunaRoute(model="glm-5.3")
    first = await _start_first_call(model)
    second = await _start_first_call(model)
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 2


def test_limit_defaults_to_one(env):
    assert LunaRoute.max_parallel() == 1


def test_zero_means_no_limit(env):
    env["LUNA_ROUTE_MAX_PARALLEL"] = "0"
    assert LunaRoute.max_parallel() is None


def test_unparseable_limit_falls_back_to_the_default():
    with patch.object(ml_models, "get_env_variable", return_value="lots"):
        assert max_parallel_from_env("X", 1) == 1


def test_gate_without_a_limit_always_admits():
    gate = LocalConcurrencyGate()
    assert all(gate.try_acquire("k", None) for _ in range(5))


def test_gate_refuses_past_its_limit():
    gate = LocalConcurrencyGate()
    assert (gate.try_acquire("k", 1), gate.try_acquire("k", 1)) == (True, False)

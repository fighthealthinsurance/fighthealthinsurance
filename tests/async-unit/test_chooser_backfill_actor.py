"""ChooserBackfillActor's run guard, the ref retiring a finished actor, and
the launch command's argument checks, without Ray."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from fighthealthinsurance.base_actor_ref import RUN_ALREADY_STARTED
from fighthealthinsurance.chooser_backfill_actor import ChooserBackfillActor
from fighthealthinsurance.chooser_backfill_actor_ref import ChooserBackfillActorRef

# The class under the Ray decorator (see test_chooser_refill_actor.py).
_Klass = getattr(ChooserBackfillActor, "__ray_metadata__", None)
_Underlying = _Klass.modified_class if _Klass else ChooserBackfillActor


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = info

    def opt(self, **k):
        return self


def _actor(running=False):
    # __new__ bypasses __init__ (Django bootstrap).
    actor = _Underlying.__new__(_Underlying)
    actor._logger = _Logger()
    actor.running = running
    actor.result = None
    return actor


def _backfill(**kwargs):
    return patch(
        "fighthealthinsurance.chooser_tasks.launch_backfill", new=AsyncMock(**kwargs)
    )


@pytest.mark.asyncio
async def test_a_second_backfill_call_is_declined_at_once():
    """A launch job attaching to an actor still busy with an earlier
    launch's backfill must not start another."""
    actor = _actor(running=True)
    with _backfill(side_effect=AssertionError("the backfill ran")):
        assert await actor.backfill() == RUN_ALREADY_STARTED
    assert await actor.is_running() is True


@pytest.mark.asyncio
async def test_the_backfill_runs_once_with_its_arguments():
    actor = _actor()
    with _backfill(return_value={"requested": 2, "ready": 2}) as backfill:
        result = await actor.backfill(count=2, min_votes=3)

    assert result == {"requested": 2, "ready": 2}
    backfill.assert_awaited_once_with(count=2, min_votes=3)
    assert await actor.is_running() is False


@pytest.mark.asyncio
async def test_a_failing_backfill_is_reported_and_the_actor_is_free_again():
    actor = _actor()
    with _backfill(side_effect=RuntimeError("database away")):
        result = await actor.backfill()

    assert result == {"error": "database away"}
    assert await actor.is_running() is False


def _ray(existing=None, running=None):
    """A ``ray`` module double: ``get_actor`` finds ``existing`` (or raises
    ValueError), whose ``is_running`` answers ``running`` (an exception
    when None); after ``kill`` the name is free."""
    module = MagicMock()
    state = {"alive": existing is not None}

    def get_actor(name, namespace):
        if not state["alive"]:
            raise ValueError("no such actor")
        return existing

    def get(ref, timeout=None):
        if running is None:
            raise RuntimeError("actor gone")
        return running

    def kill(handle, no_restart):
        state["alive"] = False

    module.get_actor.side_effect = get_actor
    module.get.side_effect = get
    module.kill.side_effect = kill
    return module


def _retire(ray_module, cluster=True):
    ref = ChooserBackfillActorRef()
    with patch(
        "fighthealthinsurance.chooser_backfill_actor_ref.ray", ray_module
    ), patch(
        "fighthealthinsurance.chooser_backfill_actor_ref.ray_cluster_available",
        return_value=cluster,
    ), patch(
        "fighthealthinsurance.chooser_backfill_actor_ref.time.sleep"
    ):
        ref._retire_finished()
    return ref


def test_nothing_to_retire_without_a_cluster_or_an_actor():
    ray_module = _ray(existing=None)
    _retire(ray_module, cluster=False)
    ray_module.get_actor.assert_not_called()

    _retire(ray_module)
    ray_module.kill.assert_not_called()


def test_an_actor_still_writing_is_left_to_finish():
    ray_module = _ray(existing=MagicMock(), running=True)
    _retire(ray_module)
    ray_module.kill.assert_not_called()


def test_a_finished_actor_is_killed_so_the_launch_gets_a_fresh_one():
    """A detached actor runs the code it was created with; a finished one
    from an earlier deploy would run that deploy's chooser."""
    ray_module = _ray(existing=MagicMock(), running=False)
    _retire(ray_module)
    ray_module.kill.assert_called_once()
    assert ray_module.kill.call_args.kwargs == {"no_restart": True}


def test_an_actor_that_does_not_answer_is_replaced():
    ray_module = _ray(existing=MagicMock(), running=None)
    with patch(
        "fighthealthinsurance.chooser_backfill_actor_ref._appeal_generation_underway",
        return_value=False,
    ):
        _retire(ray_module)
    ray_module.kill.assert_called_once()


def test_a_silent_actor_is_left_be_while_the_appeal_claim_is_held():
    """A slow answer is not a dead actor: one busy writing holds the claim,
    and killing it would leave its task half written."""
    ray_module = _ray(existing=MagicMock(), running=None)
    with patch(
        "fighthealthinsurance.chooser_backfill_actor_ref._appeal_generation_underway",
        return_value=True,
    ):
        _retire(ray_module)
    ray_module.kill.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_import_does_not_leave_the_actor_marked_running():
    """Raised before the backfill's try, ``running`` stayed set and every
    later launch was declined as a second run."""
    import builtins

    actor = _actor()
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "fighthealthinsurance.chooser_tasks":
            raise ImportError("broken deploy")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=failing_import):
        result = await actor.backfill()

    assert "error" in result
    assert await actor.is_running() is False


@pytest.mark.parametrize("min_votes", ["0", "-1"])
def test_the_command_rejects_a_vote_bar_below_one(min_votes):
    """Below one, every pool reads as fully judged and the whole backfill
    would be written before anyone voted."""
    with _backfill() as backfill, pytest.raises(CommandError):
        call_command("launch_chooser_backfill", "--inline", "--min-votes", min_votes)
    backfill.assert_not_awaited()


def _launch(result):
    """The launch command's output when the actor's backfill returns
    ``result``, with Ray stood in for."""
    from io import StringIO

    from fighthealthinsurance.chooser_backfill_actor_ref import (
        chooser_backfill_actor_ref,
    )

    out, err = StringIO(), StringIO()
    task = object()
    with patch("ray.is_initialized", return_value=True), patch(
        "ray.wait", return_value=([task], [])
    ), patch("ray.get", return_value=result), patch.object(
        chooser_backfill_actor_ref, "start", return_value=(MagicMock(), task)
    ):
        call_command("launch_chooser_backfill", stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_a_failed_backfill_is_reported_as_a_failure():
    """The actor returns {"error": ...} rather than raising; the launch
    job's log used to print it as a success."""
    out, err = _launch({"error": "database away"})

    assert "failed: database away" in err
    assert "database away" not in out


def test_a_finished_backfill_is_reported_as_done():
    out, err = _launch({"requested": 2, "ready": 2})

    assert "'ready': 2" in out
    assert not err

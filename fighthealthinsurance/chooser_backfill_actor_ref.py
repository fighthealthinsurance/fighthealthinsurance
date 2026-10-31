"""Reference to the one-shot chooser backfill actor (see
chooser_backfill_actor.py)."""

import time

import ray
from loguru import logger

from fighthealthinsurance.base_actor_ref import (
    _NAME_RELEASE_WAIT_SECONDS,
    BaseActorRef,
    ray_cluster_available,
)
from fighthealthinsurance.chooser_backfill_actor import ChooserBackfillActor


class ChooserBackfillActorRef(BaseActorRef):
    actor_class = ChooserBackfillActor
    actor_name = "chooser_backfill_actor"
    has_run_method = False

    def start(self, count=None, min_votes=None):
        """Start a backfill on a fresh actor, or on the one an earlier
        launch left still writing.

        Returns ``(actor, task)``. An actor still running an earlier
        launch's backfill declines the call at once (the task resolves to
        RUN_ALREADY_STARTED) and carries on.
        """
        self._retire_finished()
        # ``BaseActorRef.get`` is a ``cached_property``; access without parens.
        actor = self.get
        try:
            task = actor.backfill.remote(count, min_votes)
        except Exception:
            # The dispatch is the first use of the handle, so it is where a
            # creation that only half succeeded surfaces. Forget it rather
            # than keeping a dead handle for the life of the process.
            self.invalidate()
            raise
        logger.info(f"Started chooser backfill task: {task}")
        return (actor, task)

    def _retire_finished(self) -> None:
        """Kill the actor an earlier launch left behind once its backfill
        is done, so ``get`` creates one running this deploy's code: a
        detached actor runs the code it was created with until it is gone,
        and the prefetch actors have been attaching to one from an earlier
        deploy. One still writing is left to it (``backfill`` declines a
        second run), and one that does not answer is replaced. Waits
        briefly for the name to be released, as ``_replace`` does, so
        ``get`` does not hand the dying actor back.
        """
        if not ray_cluster_available():
            return
        try:
            handle = ray.get_actor(self.actor_name, namespace="fhi")
        except ValueError:
            return
        except Exception as e:
            logger.debug(f"Could not look up existing actor {self.actor_name}: {e}")
            return
        try:
            if bool(ray.get(handle.is_running.remote(), timeout=10)):
                logger.info(
                    f"Actor {self.actor_name} is still writing an earlier "
                    "launch's backfill; leaving it to finish"
                )
                return
            why = "has finished its backfill"
        except Exception as e:
            # A slow answer is not a dead actor: one busy writing holds the
            # appeal generation claim (renewed while it writes), and killing
            # it would leave its task half written.
            if _appeal_generation_underway():
                logger.info(
                    f"Actor {self.actor_name} did not answer ({e}), but the "
                    "appeal generation claim is held; leaving it be"
                )
                return
            why = f"did not answer ({e})"
        logger.info(
            f"Actor {self.actor_name} {why}; retiring it so this launch gets "
            "one running its own code"
        )
        try:
            ray.kill(handle, no_restart=True)
        except Exception as e:
            logger.warning(f"Could not kill actor {self.actor_name}: {e}")
            return
        deadline = time.monotonic() + _NAME_RELEASE_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                ray.get_actor(self.actor_name, namespace="fhi")
            except ValueError:
                break
            except Exception as e:
                logger.debug(f"Could not look up killed actor {self.actor_name}: {e}")
                break
            time.sleep(0.5)
        else:
            logger.warning(
                f"Actor {self.actor_name}'s name was still taken "
                f"{_NAME_RELEASE_WAIT_SECONDS:.0f}s after killing it"
            )
        self.invalidate()


def _appeal_generation_underway() -> bool:
    """Whether some process holds the appeal generation claim now (see
    chooser_tasks.generation_underway). Imported here, not at module scope:
    chooser_tasks pulls in the ML router, which this ref does not need."""
    from fighthealthinsurance.chooser_tasks import generation_underway

    return generation_underway("appeal")


chooser_backfill_actor_ref = ChooserBackfillActorRef()

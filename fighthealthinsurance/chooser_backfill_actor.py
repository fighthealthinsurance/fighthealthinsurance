"""The chooser's launch-time backfill, as a one-shot Ray actor.

The deploy's actor-launch job (``POLLING_ACTORS`` in scripts/start-server.sh)
starts it through ``manage.py launch_chooser_backfill`` and moves on; the
actor runs ``chooser_tasks.launch_backfill`` (a hundred appeal cases, one at
a time, once the voters have judged every case the current pipeline made)
and returns what happened. The refill actor, which tops the pool up ten
tasks at a time, is a separate, long-running actor.

A detached actor runs the code it was created with until it is gone, so a
finished one left behind would run an earlier deploy's chooser at the next
launch: the ref retires it first (chooser_backfill_actor_ref.py). One still
writing is left to finish, and declines a second backfill.

loguru is imported inside the methods, not at module scope: Ray pickles the
class by value, and a module-level logger carries the process's sinks, whose
locks do not pickle (tests/sync/test_actor_classes_survive_pickling.py).
"""

import os
from typing import Any, Optional

import ray

from fighthealthinsurance.base_actor_ref import decline_second_run
from fighthealthinsurance.utils import get_env_variable

name = "ChooserBackfillActor"


# No restarts: a backfill cut short by a lost node waits for the next
# deploy, whose launch job asks again whether one is due.
@ray.remote(max_restarts=0, max_task_retries=0)
class ChooserBackfillActor:
    def __init__(self):
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        from loguru import logger

        self._logger = logger
        self.running = False
        self.result: Optional[dict] = None
        self._logger.info("ChooserBackfillActor initialized")

    async def is_running(self) -> bool:
        """Whether a backfill is being written now (the ref asks before
        retiring an actor from an earlier launch)."""
        return self.running

    async def backfill(
        self, count: Optional[int] = None, min_votes: Optional[int] = None
    ) -> Any:
        """Run the backfill once (see chooser_tasks.launch_backfill) and
        return what happened. A second call while it runs is declined, as a
        loop actor's ``run`` is: a launch job attaching to an actor still
        busy with an earlier launch's backfill must not start another."""
        if self.running:
            return decline_second_run(self._logger, "ChooserBackfillActor", "backfill")
        self.running = True
        self._logger.info("Starting the chooser backfill")
        try:
            # Inside the try: an import that raised used to leave ``running``
            # set for the life of the actor, so every later launch was
            # declined as a second run.
            from fighthealthinsurance.chooser_tasks import launch_backfill

            self.result = await launch_backfill(count=count, min_votes=min_votes)
            self._logger.info(f"Chooser backfill finished: {self.result}")
        except Exception as e:
            self._logger.opt(exception=True).error(f"Chooser backfill failed: {e}")
            self.result = {"error": str(e)}
        finally:
            self.running = False
        return self.result

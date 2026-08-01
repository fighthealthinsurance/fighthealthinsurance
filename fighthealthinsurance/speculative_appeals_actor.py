"""Detached Ray actor that precomputes bare, internal-only candidate appeals.

Dispatched fire-and-forget with just ``denial_id`` from denial creation (see
ml_speculative_appeals_helper.dispatch_speculative_appeals). The actual work
lives in the plain, unit-testable ``SpeculativeAppealsHelper`` so it can run
either here (in this actor's process) or, when Ray is unavailable, in a daemon
thread -- this class is only the Django-booting Ray wrapper, mirroring the
other detached actors (e.g. UCRRefreshActor).
"""

import os
import time
from typing import Any

import ray

from fighthealthinsurance.utils import get_env_variable

name = "SpeculativeAppealsActor"


# max_concurrency: this is an ASYNC actor (prefetch_for_denial is a coroutine),
# and Ray's default concurrency for those is 1000 -- fine for the other async
# actors, whose per-task work is cheap, but each task here is a FULL
# make_appeals ML fan-out plus its own non-thread-sensitive pool thread, and
# they all land in this one process on a Ray worker capped at 1 CPU / 6-7G. A
# burst of denial submissions would otherwise pile hundreds of concurrent
# generations into it. 10 keeps precomputes from serializing behind each other
# (the reason generate_for_denial bridges with thread_sensitive=False) while
# bounding the process. Excess dispatches queue rather than being dropped, and
# the helper re-checks on dequeue whether the denial already has appeals, so a
# backlog sheds the ones speculation is too late to help.
#
# The ignore below is needed because ray's bundled stub omits max_concurrency
# from the ray.remote(...) overload. The runtime does accept it -- it lands in
# the ActorClass's _default_options, which the test pins -- so the stub is
# simply incomplete, same reason base_actor_ref ignores .options().
@ray.remote(  # type: ignore[call-overload]
    max_restarts=-1, max_task_retries=-1, max_concurrency=10
)
class SpeculativeAppealsActor:
    def __init__(self):
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        from loguru import logger

        self._logger = logger
        logger.info("SpeculativeAppealsActor initialized")

    async def hello(self) -> str:
        return "Hi"

    async def prefetch_for_denial(
        self,
        denial_id: Any,
        force: bool = False,
        trigger: str = "denial_created",
        confirmed_context: bool = False,
    ) -> int:
        """Generate + persist speculative candidate appeals for a denial.

        The helper is natively async, so it is awaited directly: it bridges only
        the blocking generation itself to a thread (non-thread-sensitive, so a
        burst of denial creations doesn't serialize onto one executor thread --
        see generate_for_denial) and keeps every query on the async ORM.

        ``trigger``/``confirmed_context`` select and label the precompute round
        (create-time bare pass vs. the post-confirmation refresh) -- see
        SpeculativeAppealsHelper.generate_for_denial.
        """
        from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
            SpeculativeAppealsHelper,
        )

        return await SpeculativeAppealsHelper.generate_for_denial(
            denial_id,
            force=force,
            trigger=trigger,
            confirmed_context=confirmed_context,
        )

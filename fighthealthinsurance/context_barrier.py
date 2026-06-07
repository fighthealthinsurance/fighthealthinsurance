"""Bounded-wait barrier for cached context fields on a ``Denial``.

When the user reaches the appeal-generation step, ``PubMed`` and
``ml_citation`` lookups may already be in flight as fire-and-forget tasks
launched during entity extraction.  Those background tasks write their
results to columns on the ``Denial`` row; the cache-aware inline fetchers
(``PubMedTools.find_context_for_denial`` /
``MLCitationsHelper.generate_citations_for_denial``) short-circuit on those
columns — but they check the *in-memory* ``denial`` instance, which was
loaded before the background task finished and is therefore stale.

So the barrier does two things: (1) wait a bounded time for the background
task to populate its column(s), then (2) refresh those columns onto the
in-memory ``denial`` so the subsequent inline fetch short-circuits instead
of regenerating.  Crucially the *readiness* columns are the ones the
background task actually writes — for citations that is
``candidate_ml_citation_context`` (the speculative task stores there), not
``ml_citation_context``.  After refresh we always run the inline fetch,
which reuses the helper's own freshness/promotion logic rather than
duplicating it here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Sequence

from loguru import logger


def _is_populated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return len(value) > 0
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


async def _any_field_populated(denial_id: int, fields: Sequence[str]) -> bool:
    """True if any of ``fields`` on the Denial row is non-empty.

    One ``values()`` query covers all fields so a multi-field readiness
    check is a single round-trip.
    """
    # Local import keeps the module importable in non-Django contexts (tests
    # patch this function directly).
    from fighthealthinsurance.models import Denial

    row = await Denial.objects.filter(denial_id=denial_id).values(*fields).afirst()
    if not row:
        return False
    return any(_is_populated(row.get(f)) for f in fields)


async def wait_for_warm_cache(
    denial: Any,
    *,
    readiness_fields: Sequence[str],
    refresh_fields: Sequence[str],
    barrier_timeout: float,
    poll_interval: float = 1.0,
) -> bool:
    """Wait up to ``barrier_timeout`` for an in-flight background task to
    populate any of ``readiness_fields`` on the denial's DB row.

    On readiness, refresh ``refresh_fields`` onto the in-memory ``denial``
    so a subsequent cache-aware fetch short-circuits.  Returns ``True`` on
    readiness, ``False`` on timeout.  Never raises — a DB hiccup degrades to
    "not ready" and the caller falls through to a normal (possibly
    regenerating) fetch.
    """
    denial_id = denial.denial_id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, barrier_timeout)
    hit = False
    try:
        hit = await _any_field_populated(denial_id, readiness_fields)
        while not hit and loop.time() < deadline:
            await asyncio.sleep(poll_interval)
            hit = await _any_field_populated(denial_id, readiness_fields)
    except Exception as e:
        logger.opt(exception=True).debug(
            f"Cache barrier poll failed for denial {denial_id} "
            f"fields={list(readiness_fields)}: {e}"
        )
        return False

    if hit:
        try:
            await denial.arefresh_from_db(fields=list(refresh_fields))
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Cache barrier refresh failed for denial {denial_id}: {e}"
            )
    else:
        logger.debug(
            f"Cache barrier timed out for denial {denial_id} after "
            f"{barrier_timeout}s; falling through to inline fetch"
        )
    return hit


async def warm_then_fetch(
    denial: Any,
    *,
    readiness_fields: Sequence[str],
    refresh_fields: Sequence[str],
    barrier_timeout: float,
    fetch: Callable[[], Awaitable[Any]],
    poll_interval: float = 1.0,
) -> Any:
    """Wait briefly for the background cache to warm, then run ``fetch``.

    ``fetch`` is the cache-aware inline fetcher (wrapped in the caller's
    ``tracked_awaitable`` so it still streams its own FE status frame).  On
    a warm cache it short-circuits and returns instantly; on a cold cache
    (barrier timed out) it regenerates as before.
    """
    await wait_for_warm_cache(
        denial,
        readiness_fields=readiness_fields,
        refresh_fields=refresh_fields,
        barrier_timeout=barrier_timeout,
        poll_interval=poll_interval,
    )
    return await fetch()

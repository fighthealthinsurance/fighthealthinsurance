"""Bounded-wait barrier for cached context fields on a ``Denial``.

When the user reaches the appeal-generation step, ``PubMed`` and
``ml_citation`` lookups may already be in flight as fire-and-forget tasks
launched during entity extraction.  The downstream helpers
(``PubMedTools.find_context_for_denial`` /
``MLCitationsHelper.generate_citations_for_denial``) short-circuit on the
cached value, but only after a slow inline call has at least started.  We
want to wait briefly for the in-flight task to finish so the inline call
isn't duplicate work, while still communicating progress to the frontend
and giving up after a bounded number of seconds.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

_BARRIER_PHASE = "research"


async def _read_field(denial_id: int, field_name: str) -> Any:
    """Read a single field from the Denial row without loading the whole instance."""
    # Local import keeps the module importable in non-Django contexts (tests).
    from fighthealthinsurance.models import Denial

    row = await Denial.objects.filter(denial_id=denial_id).values(field_name).afirst()
    return row[field_name] if row else None


def _is_populated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return len(value) > 0
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


async def _emit_status(
    queue: Optional[asyncio.Queue[str]],
    *,
    substep: str,
    state: str,
    message: str,
) -> None:
    if queue is None:
        return
    await queue.put(
        json.dumps(
            {
                "type": "status",
                "phase": _BARRIER_PHASE,
                "substep": substep,
                "state": state,
                "message": message,
            }
        )
    )


async def with_warm_cache(
    denial_id: int,
    field_name: str,
    *,
    barrier_timeout: float,
    poll_interval: float,
    status_queue: Optional[asyncio.Queue[str]],
    substep: str,
    ready_msg: str,
    fallback_factory: Callable[[], Awaitable[Any]],
) -> Any:
    """Wait up to ``barrier_timeout`` seconds for ``Denial[field_name]`` to
    become non-empty.  On a cache hit, emit a ``state="done"`` frame and
    return the cached value.  On miss, await ``fallback_factory()`` (which
    should itself stream its own status frames).

    The frame schema matches what ``appeal_fetcher.ts`` already expects:
    ``{type, phase="research", substep, state="done", message}``.  No
    new field shapes — the FE renders unchanged.
    """
    deadline = asyncio.get_event_loop().time() + max(0.0, barrier_timeout)
    # Cheap upfront check — avoids sleeping when the cache is already warm
    # from a previous attempt.
    try:
        cached = await _read_field(denial_id, field_name)
    except Exception as e:
        logger.opt(exception=True).debug(
            f"Cache barrier initial read failed for {field_name}: {e}"
        )
        cached = None
    if _is_populated(cached):
        await _emit_status(
            status_queue, substep=substep, state="done", message=ready_msg
        )
        return cached

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            cached = await _read_field(denial_id, field_name)
        except Exception as e:
            logger.opt(exception=True).debug(
                f"Cache barrier poll read failed for {field_name}: {e}"
            )
            cached = None
        if _is_populated(cached):
            await _emit_status(
                status_queue, substep=substep, state="done", message=ready_msg
            )
            return cached

    # Cache miss: fall through to the inline awaitable. The inline call's
    # own tracked_awaitable will emit its completion frame, so no
    # additional status here.
    logger.debug(
        f"Cache barrier timed out for {field_name} on denial {denial_id} "
        f"after {barrier_timeout}s; falling through to inline fetch"
    )
    return await fallback_factory()

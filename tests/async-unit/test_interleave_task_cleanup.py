"""The keep-alive interleaver must not outlive itself.

``_interleave_iterator_for_keep_alive`` drives its source with an explicit
Task so a keep-alive timeout does not lose the in-flight item. Closing the
generator -- which the appeal websocket consumer does in its ``finally`` every
time a client hangs up mid-generation -- throws ``GeneratorExit`` at the
suspension point. ``GeneratorExit`` is a ``BaseException``, so no
``except Exception`` clause ever saw it, and the in-flight ``__anext__()``
task was left running with nobody to await it.

It then finished on its own, usually with ``StopAsyncIteration``, and asyncio
reported the unread exception at GC: a stack-less ``StopAsyncIteration`` from
the ``asyncio`` logger a minute or two after a disconnect, which is exactly
how long the abandoned model call took to finish (PYTHON-DJANGO-00-4Z).
"""

import asyncio
import gc

import pytest

from fighthealthinsurance.utils import (
    _interleave_iterator_for_keep_alive,
    _retire_anext_task,
)


async def _drain_until_task_is_in_flight(agen, source_started):
    """Consume the leading keep-alive newlines and wait for the source to run.

    The generator yields a newline, dispatches the source's ``__anext__()``
    as a Task, then yields a second newline -- so after two items it is
    suspended with a live task behind it, which is the state that used to
    leak.
    """
    assert await agen.__anext__() == "\n"
    assert await agen.__anext__() == "\n"
    await asyncio.wait_for(source_started.wait(), timeout=5)


@pytest.mark.asyncio
async def test_closing_mid_stream_leaves_no_running_anext_task():
    source_started = asyncio.Event()

    async def never_finishes():
        source_started.set()
        await asyncio.sleep(300)
        yield "unreachable"

    agen = _interleave_iterator_for_keep_alive(never_finishes(), timeout=60)
    await _drain_until_task_is_in_flight(agen, source_started)

    in_flight = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    assert in_flight, "the interleaver should have dispatched an __anext__ task"

    await agen.aclose()
    # One turn for the cancellation the close scheduled to be delivered.
    await asyncio.sleep(0)

    still_running = [t for t in in_flight if not t.done()]
    assert still_running == [], f"orphaned task(s) survived aclose(): {still_running}"


@pytest.mark.asyncio
async def test_a_completed_stream_still_yields_every_item():
    """The cleanup must not cost the normal path anything."""
    async def three_items():
        for item in ("a", "b", "c"):
            yield item

    got = [
        chunk
        async for chunk in _interleave_iterator_for_keep_alive(three_items(), timeout=60)
    ]
    assert [chunk for chunk in got if chunk != "\n"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_retiring_a_finished_failed_task_silences_the_gc_report():
    """The exact shape Sentry saw: a finished task whose exception nobody read."""
    loop = asyncio.get_running_loop()
    reported: list = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: reported.append(ctx))
    try:

        async def raises_stop_async_iteration():
            raise StopAsyncIteration

        task = asyncio.ensure_future(raises_stop_async_iteration())
        await asyncio.wait([task])
        # Deliberately NOT calling task.exception() here: reading it is what
        # marks it retrieved, so doing so would make this test vacuous.
        _retire_anext_task(task)
        del task
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    never_retrieved = [
        ctx for ctx in reported if "never retrieved" in str(ctx.get("message", ""))
    ]
    assert never_retrieved == []


@pytest.mark.asyncio
async def test_retiring_is_safe_for_a_cancelled_task():
    """``task.exception()`` raises for a cancelled task; retiring must not."""
    async def sleeps():
        await asyncio.sleep(300)

    task = asyncio.ensure_future(sleeps())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait([task])
    assert task.cancelled()
    _retire_anext_task(task)  # must not raise


@pytest.mark.asyncio
async def test_retiring_none_is_a_no_op():
    _retire_anext_task(None)

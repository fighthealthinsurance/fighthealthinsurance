"""FlexibleFaxMagic gives each fax backend BACKEND_SEND_TIMEOUT_SECONDS across
its attempts, then moves on to the next. The limit is asyncio's: the send runs
on the event loop, where a thread timeout (the old stopit one) raised into
whichever task happened to be running."""

import asyncio
from unittest.mock import patch

import pytest

from fighthealthinsurance.fax_status import FAILURE_OUR_SIDE
from fighthealthinsurance.fax_utils import (
    FaxSenderBase,
    FaxSendReport,
    FlexibleFaxMagic,
)


class _HangingBackend(FaxSenderBase):
    """Never answers within the (patched-down) per-backend limit."""

    async def send_fax_blocking(self, destination, path, dest_name=None):
        await asyncio.sleep(60)
        return True


class _WorkingBackend(FaxSenderBase):
    def __init__(self) -> None:
        self.calls = 0

    async def send_fax_blocking(self, destination, path, dest_name=None):
        self.calls += 1
        return True


async def _send(backends):
    report = FaxSendReport()

    async def _one_transmission(*args, **kwargs):
        return ["/tmp/does-not-matter.pdf"]

    with (
        patch.object(FlexibleFaxMagic, "assemble_outputs", _one_transmission),
        patch("fighthealthinsurance.fax_utils.PdfReader") as reader,
        patch("fighthealthinsurance.fax_utils.BACKEND_SEND_TIMEOUT_SECONDS", 0.1),
        patch("fighthealthinsurance.fax_utils.time.sleep"),
    ):
        reader.return_value.pages = [object()]
        sent = await FlexibleFaxMagic(list(backends)).send_fax(
            input_paths=["/tmp/letter.pdf"],
            extra="",
            destination="14255555555",
            blocking=True,
            professional=False,
            report=report,
        )
    return sent, report


@pytest.mark.asyncio
async def test_backend_past_its_time_limit_is_abandoned_for_the_next():
    working = _WorkingBackend()
    # estimate_cost ties, and sorted() is stable: the hanging one goes first.
    sent, _report = await _send([_HangingBackend(), working])
    assert (sent, working.calls) == (True, 1)


@pytest.mark.asyncio
async def test_backend_past_its_time_limit_is_noted_as_our_side():
    sent, report = await _send([_HangingBackend()])
    assert (sent, report.failure_reason) == (False, FAILURE_OUR_SIDE)


@pytest.mark.asyncio
async def test_backend_time_limit_leaves_other_tasks_on_the_loop_alone():
    async def bystander():
        await asyncio.sleep(0.3)
        return "finished"

    task = asyncio.create_task(bystander())
    await _send([_HangingBackend()])
    assert await task == "finished"

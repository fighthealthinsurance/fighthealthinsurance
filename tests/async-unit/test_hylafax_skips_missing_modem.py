"""A HylaFAX backend checks its modem is attached to the fax host before it
queues a fax. When a USB modem drops off the host, HylaFAX keeps accepting
jobs that nothing will ever send, and ``sendfax -w`` waits out the whole send
timeout; checking the device first lets the next backend take the fax at once.
The result is recorded for the staff status page."""

from unittest.mock import AsyncMock, patch

import pytest

from fighthealthinsurance.fax_status import FAILURE_OUR_SIDE
from fighthealthinsurance.fax_utils import FaxSendReport, _current_report
from tests.hylafax_fakes import FakeFaxHost, send_letter

_HEALTH = "fighthealthinsurance.models.ExternalServiceHealth"


@pytest.mark.asyncio
async def test_a_fax_with_its_modem_missing_fails_without_queueing(tmp_path):
    client = FakeFaxHost(devices=set())
    assert await send_letter(client, tmp_path) is False


@pytest.mark.asyncio
async def test_a_fax_with_its_modem_missing_never_runs_sendfax(tmp_path):
    client = FakeFaxHost(devices=set())
    await send_letter(client, tmp_path)
    assert client.sendfax_commands() == []


@pytest.mark.asyncio
async def test_a_fax_with_its_modem_missing_uploads_nothing(tmp_path):
    client = FakeFaxHost(devices=set())
    await send_letter(client, tmp_path)
    assert client.uploaded() == set()


@pytest.mark.asyncio
async def test_a_fax_with_its_modem_attached_is_sent_on_that_modem(tmp_path):
    client = FakeFaxHost()
    assert await send_letter(client, tmp_path) is True
    assert "-httyACM0@localhost" in client.sendfax_commands()[0]


@pytest.mark.asyncio
async def test_a_missing_mapped_modem_is_skipped_for_an_attached_default(tmp_path):
    client = FakeFaxHost(devices={"/dev/ttyACM0"})
    client.modem_mapping = {"1425": ["ttyIAX0"]}
    await send_letter(client, tmp_path)
    sendfax = client.sendfax_commands()[0]
    assert "-httyIAX0@localhost" not in sendfax


@pytest.mark.asyncio
async def test_an_unnamed_modem_is_left_to_hylafax_unchecked(tmp_path):
    client = FakeFaxHost(devices=set())
    client.default_modem = None
    assert await send_letter(client, tmp_path) is True


@pytest.mark.asyncio
async def test_a_missing_modem_is_noted_as_our_side_on_the_send_report(tmp_path):
    report = FaxSendReport()
    token = _current_report.set(report)
    try:
        await send_letter(FakeFaxHost(devices=set()), tmp_path)
    finally:
        _current_report.reset(token)
    assert report.failure_reason == FAILURE_OUR_SIDE


@pytest.mark.asyncio
async def test_a_missing_modem_is_recorded_for_the_status_page(tmp_path):
    client = FakeFaxHost(devices=set())
    client.modem_health_service = "fax_modem_test"
    with patch(f"{_HEALTH}.anote_failure", new_callable=AsyncMock) as failure:
        await send_letter(client, tmp_path)
    failure.assert_awaited_once_with("fax_modem_test", "ttyACM0 missing")


@pytest.mark.asyncio
async def test_an_unreachable_fax_host_is_recorded_as_unchecked(tmp_path):
    client = FakeFaxHost(reachable=False)
    client.modem_health_service = "fax_modem_test"
    with patch(f"{_HEALTH}.anote_failure", new_callable=AsyncMock) as failure:
        await send_letter(client, tmp_path)
    failure.assert_awaited_once_with("fax_modem_test", "ttyACM0 unchecked")


@pytest.mark.asyncio
async def test_an_attached_modem_is_recorded_for_the_status_page(tmp_path):
    client = FakeFaxHost()
    client.modem_health_service = "fax_modem_test"
    with patch(f"{_HEALTH}.anote_success", new_callable=AsyncMock) as success:
        await send_letter(client, tmp_path)
    success.assert_awaited_once_with("fax_modem_test")


@pytest.mark.asyncio
async def test_a_backend_without_a_health_service_records_nothing(tmp_path):
    client = FakeFaxHost(devices=set())
    with patch(f"{_HEALTH}.anote_failure", new_callable=AsyncMock) as failure:
        await send_letter(client, tmp_path)
    failure.assert_not_awaited()

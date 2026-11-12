"""The SSH HylaFAX backend copies the fax and a file holding the destination
number to the fax host for sendfax. Both copies are removed from the fax host
once sendfax has returned, whether the fax went through or not, since sendfax
has stored its own copy on the fax server by then."""

import pytest

from tests.hylafax_fakes import FakeFaxHost as _FaxHost
from tests.hylafax_fakes import send_letter as _send


@pytest.mark.asyncio
async def test_a_sent_fax_is_removed_from_the_fax_host(tmp_path):
    client = _FaxHost()
    await _send(client, tmp_path)
    # The destination file and the fax.
    assert len(client.uploaded()) == 2
    assert client.removed() == client.uploaded()


@pytest.mark.asyncio
async def test_a_fax_that_fails_to_send_is_removed_from_the_fax_host(tmp_path):
    client = _FaxHost(sendfax_exit_code=1)
    await _send(client, tmp_path)
    assert len(client.uploaded()) == 2
    assert client.removed() == client.uploaded()


@pytest.mark.asyncio
async def test_the_copies_are_removed_only_after_sendfax_has_run(tmp_path):
    client = _FaxHost()
    await _send(client, tmp_path)
    programs = [command[0] for command in client.commands]
    last_sendfax = len(programs) - 1 - programs[::-1].index("sendfax")
    assert "rm" not in programs[:last_sendfax]


@pytest.mark.asyncio
async def test_a_fax_whose_upload_fails_partway_is_removed_from_the_fax_host(
    tmp_path,
):
    client = _FaxHost()
    # The destination file goes up; the fax fails partway through its copy.
    client.sftp.put.side_effect = [None, OSError("connection lost")]
    await _send(client, tmp_path)
    assert len(client.uploaded()) == 2
    assert client.removed() == client.uploaded()

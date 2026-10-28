"""The SSH HylaFAX backend copies the fax and a file holding the destination
number to the fax host for sendfax. Both copies are removed from the fax host
once sendfax has returned, whether the fax went through or not, since sendfax
has stored its own copy on the fax server by then."""

from unittest.mock import Mock

import pytest

from fighthealthinsurance.fax_utils import SshHylaFaxClient
from tests.pdf_fixtures import make_pdf_bytes


class _FaxHost(SshHylaFaxClient):
    """An SSH HylaFAX client whose SSH and SFTP connections are stand-ins,
    recording each remote command."""

    host = "localhost"
    remote_host = "fax-host.example"

    def __init__(self, sendfax_exit_code: int = 0):
        self.sendfax_exit_code = sendfax_exit_code
        self.commands: list[list[str]] = []
        self.sftp = Mock()

    def _create_ssh_client(self):
        ssh = Mock()
        ssh.open_sftp.return_value = self.sftp
        return ssh

    async def _run_command(self, command: list[str]) -> tuple[int, str]:
        self.commands.append(command)
        if command[0] == "sendfax":
            return (self.sendfax_exit_code, "STDOUT: request id is 42 STDERR: ")
        if command[0] == "faxstat":
            return (0, "STDOUT: JID Pri S\n42 127 D\n STDERR: ")
        return (0, "")

    def uploaded(self) -> set[str]:
        return {call.args[1] for call in self.sftp.put.call_args_list}

    def removed(self) -> set[str]:
        return {command[2] for command in self.commands if command[:2] == ["rm", "-f"]}


async def _send(client: _FaxHost, tmp_path) -> bool:
    letter = tmp_path / "combined.pdf"
    letter.write_bytes(make_pdf_bytes(["Dear reviewer"]))
    return await client.send_fax(
        destination="14255555555", path=str(letter), blocking=True
    )


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

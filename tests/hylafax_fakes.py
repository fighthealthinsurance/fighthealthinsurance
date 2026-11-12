"""A stand-in for the SSH HylaFAX fax host, shared by the HylaFAX tests."""

from typing import Optional
from unittest.mock import Mock

from fighthealthinsurance.fax_utils import SshHylaFaxClient
from tests.pdf_fixtures import make_pdf_bytes


class FakeFaxHost(SshHylaFaxClient):
    """An SSH HylaFAX client whose SSH and SFTP connections are stand-ins,
    recording each remote command.

    ``devices`` are the modem devices attached to the fax host; ``test -e``
    on any other path fails as it would there. ``reachable=False`` makes every
    remote command fail the way an ssh connection error does."""

    host = "localhost"
    remote_host = "fax-host.example"
    default_modem = "ttyACM0"

    def __init__(
        self,
        sendfax_exit_code: int = 0,
        devices: Optional[set[str]] = None,
        reachable: bool = True,
    ):
        self.sendfax_exit_code = sendfax_exit_code
        self.devices = {"/dev/ttyACM0"} if devices is None else devices
        self.reachable = reachable
        self.commands: list[list[str]] = []
        self.sftp = Mock()

    def _create_ssh_client(self):
        ssh = Mock()
        ssh.open_sftp.return_value = self.sftp
        return ssh

    async def _run_command(self, command: list[str]) -> tuple[int, str]:
        self.commands.append(command)
        if not self.reachable:
            return (254, f"connection refused from {command}")
        if command[:2] == ["test", "-e"]:
            return (0 if command[2] in self.devices else 1, "")
        if command[0] == "sendfax":
            return (self.sendfax_exit_code, "STDOUT: request id is 42 STDERR: ")
        if command[0] == "faxstat":
            return (0, "STDOUT: JID Pri S\n42 127 D\n STDERR: ")
        return (0, "")

    def uploaded(self) -> set[str]:
        return {call.args[1] for call in self.sftp.put.call_args_list}

    def removed(self) -> set[str]:
        return {command[2] for command in self.commands if command[:2] == ["rm", "-f"]}

    def sendfax_commands(self) -> list[list[str]]:
        return [command for command in self.commands if command[0] == "sendfax"]


async def send_letter(client: FakeFaxHost, tmp_path) -> bool:
    """Send a one page letter to a fixed number through ``client``."""
    letter = tmp_path / "combined.pdf"
    letter.write_bytes(make_pdf_bytes(["Dear reviewer"]))
    return await client.send_fax(
        destination="14255555555", path=str(letter), blocking=True
    )

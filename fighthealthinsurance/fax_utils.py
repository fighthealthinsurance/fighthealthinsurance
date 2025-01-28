import os
import re
import subprocess
import tempfile
import time
from typing import Mapping, Optional, Tuple
from paramiko import SSHClient
from stopit import ThreadingTimeout as Timeout
import asyncio, asyncssh
from .utils import check_call

import requests
from requests import Session
from PyPDF2 import PdfMerger, PdfReader, PdfWriter

FROM_FAX = os.getenv("FROM_FAX", "4158407591")
FROM_VOICE = os.getenv("FROM_VOICE", "2029383266")


class FaxSenderBase(object):
    base_cost = 0
    cost_per_page = 0
    # Avoid calling 211/etc.
    special_naps = re.compile(r"^1?(\d11|\d\d\d\d11)")

    def estimate_cost(self, destination: str, pages: int) -> int:
        return self.base_cost + self.cost_per_page * pages

    def parse_phone_number(self, input: str) -> str:
        """Parse a phone number, raise exception if it's a \"bad\" one."""
        numbers = re.findall(r"\d+", input)
        number_str = "".join(map(str, numbers))
        # Add 1 in front of any phone number we call
        if number_str[0] != "1":
            number_str = f"1{number_str}"
        if number_str.startswith("1900"):
            raise Exception("No calling 1900 numbers")
        if number_str.startswith("1911"):
            raise Exception("No trying to call 911 this is for faxes.")
        if number_str.startswith("10"):
            raise Exception("No trying to call the operator")
        if self.special_naps.match(number_str):
            raise Exception("No calling special svc numbers")
        return number_str

    async def send_fax(
        self,
        destination: str,
        path: str,
        dest_name: Optional[str] = None,
        blocking: bool = False,
    ) -> bool:
        destination = self.parse_phone_number(destination)
        if blocking:
            return await self.send_fax_blocking(
                destination=destination, path=path, dest_name=dest_name
            )
        else:
            return await self.send_fax_nonblocking(
                destination=destination, path=path, dest_name=dest_name
            )

    async def send_fax_blocking(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ) -> bool:
        return True

    async def send_fax_nonblocking(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ) -> bool:
        return True


class SonicFax(FaxSenderBase):
    base_cost = 10
    cost_per_page = 2
    csrf_regex = re.compile(r"\"csrfKey\" value=\"(.*?)\"")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
    }
    token: str
    password: str
    username: str

    def __init__(self):
        username = os.getenv("SONIC_USERNAME")
        if username is None:
            raise Exception("Missing sonic username")
        else:
            self.username = username
        password = os.getenv("SONIC_PASSWORD")
        if password is None:
            raise Exception("Missing sonic password")
        else:
            self.password = password
        token = os.getenv("SONIC_TOKEN")
        if token is None:
            raise Exception("We need a token SONIC_TOKEN")
        else:
            self.token = token
        self.notification_email = os.getenv(
            "SONIC_NOTIFICATION_EMAIL", "support42@fighthealthinsurance.com"
        )

    async def send_fax_blocking(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ) -> bool:
        with requests.Session() as s:
            cookies = self._login(s)
            self._send_fax_non_blocking(
                s=s,
                cookies=cookies,
                destination=destination,
                path=path,
                dest_name=dest_name,
            )
            return self._blocking_check_fax_status(
                s=s,
                cookies=cookies,
                destination=destination,
                path=path,
                dest_name=dest_name,
            )

    def _login(self, s: Session) -> dict[str, str]:
        cookies = {"mt2FAToken": self.token}
        r = s.get("https://members.sonic.net/", headers=self.headers, cookies=cookies)
        r = s.post(
            "https://members.sonic.net/",
            data={"login": "login", "user": self.username, "pw": self.password},
            headers=self.headers,
            cookies=cookies,
        )
        if "Member Login" in r.text:
            raise Exception(f"Error logging into sonic got back {r.text}")
        return cookies

    def blocking_check_fax_status(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ) -> bool:
        with requests.Session() as s:
            cookies = self._login(s)
            return self._blocking_check_fax_status(
                s, cookies, destination, path, dest_name
            )

    def _blocking_check_fax_status(
        self,
        s: Session,
        cookies: dict[str, str],
        destination: str,
        path: str,
        dest_name: Optional[str] = None,
    ) -> bool:
        r = s.get(
            "https://members.sonic.net/labs/fax/?a=history",
            headers=self.headers,
            cookies=cookies,
        )
        # Get the filename but also just the name
        filename = self._get_filename(path)
        filename = filename.split("/")[-1]
        c = 0
        max_initial_count = 5
        max_final_count = 25
        # Polling check for the filename
        while c < max_initial_count:
            r = s.get(
                "https://members.sonic.net/labs/fax/?a=history",
                headers=self.headers,
                cookies=cookies,
            )
            c = c + 1
            time.sleep(c)
            if filename in r.text:
                break
        if filename not in r.text:
            return False
        c = 0
        chunk = None
        while c < max_final_count:
            c = c + 1
            time.sleep(c)
            r = s.get(
                "https://members.sonic.net/labs/fax/?a=history",
                headers=self.headers,
                cookies=cookies,
            )
            chunks = r.text.split("tr>")
            for chunk in chunks:
                if filename in chunk and destination in chunk:
                    if ">failed<" in chunk:
                        print(f"Failed :( {chunk}")
                        return False
                    elif ">success<" in chunk:
                        print(f"Success: {chunk}")
                        return True
        print(f"Timed out! last chunk {chunk} -- {filename}")
        return False

    async def send_fax_non_blocking(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ):
        with requests.Session() as s:
            cookies = self._login(s)
            return self._send_fax_non_blocking(s, cookies, destination, path, dest_name)

    def _get_filename(self, path: str) -> str:
        _head, tail = os.path.split(path)
        return tail

    def _send_fax_non_blocking(
        self,
        s: Session,
        cookies: dict[str, str],
        destination: str,
        path: str,
        dest_name: Optional[str] = None,
    ):
        r = s.get(
            "https://members.sonic.net/labs/fax", headers=self.headers, cookies=cookies
        )
        r.raise_for_status()
        csrf_matched = self.csrf_regex.search(r.text)
        if csrf_matched is None:
            raise Exception(f"No CSRF found in {r.text}")
        csrf_key = csrf_matched.group(1)
        filename = self._get_filename(path)
        r = s.post(
            "https://members.sonic.net/labs/fax/?a=upload",
            files={"filename": (filename, open(path, "rb"))},
        )
        r.raise_for_status
        r = s.post(
            "https://members.sonic.net/labs/fax/",
            data={
                "destination": destination,
                "a": "sendFax",
                "csrfKey": csrf_key,
                "coverTo": dest_name or destination or "HealthCo",
                "coverFrom": "Fight Health Insurance",
                "MAX_FILE_SIZE": "52428800",
                "email": self.notification_email,
                "fromVoice": FROM_VOICE,
                "fromFax": FROM_FAX,
                "message": "Fight Health Insurance",
                "includeCover": "1",
            },
        )


class HylaFaxClient(FaxSenderBase):
    base_cost = 0
    cost_per_page = 0
    host: Optional[str]
    # Going above 9600 causes issues sometimes
    max_speed = 9600
    # Default is no modem specified which uses any modem on host
    default_modem: Optional[str] = None
    # Mapping from prefixes to modem names (e.g. so 1800 numbers)
    modem_mapping: Mapping[str, list[Optional[str]]] = {}

    def estimate_cost(self, destination: str, pages: int) -> int:
        if self.host is not None:
            return self.base_cost + self.cost_per_page * pages
        else:
            return 9999999999999999

    async def send_fax_blocking(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ) -> bool:
        return await self._send_fax(
            destination=destination, path=path, dest_name=dest_name, blocking=True
        )

    async def send_fax_nonblocking(
        self, destination: str, path: str, dest_name: Optional[str] = None
    ) -> bool:
        return await self._send_fax(
            destination=destination, path=path, dest_name=dest_name, blocking=False
        )

    def _choose_modems(self, destination: str) -> list[Optional[str]]:
        """Choose the modem for a diven destination, returns any mapped modems + default"""
        prefix: str = destination[0:4]
        return self.modem_mapping.get(prefix, []) + [self.default_modem]

    def _host_strings(self, destination: str) -> list[str]:
        """Compute the different host strings, this depends on if there is custom modem mappings"""
        if self.host is None:
            raise Exception("Need host")

        def __host_string(modem: Optional[str]) -> str:
            # Yes this is silly but mypy...
            if self.host is None:
                raise Exception("Need host")
            if modem is None:
                return self.host
            else:
                return f"{modem}@{self.host}"

        return list(map(__host_string, self._choose_modems(destination)))

    async def _upload_file(self, path: str) -> Optional[str]:
        """Upload files to remote host if needed. Returns None on failure. Return remote path."""
        return path

    async def _run_command(self, command: list[str]) -> Tuple[int, str]:
        """Return the command and it's output"""
        print(f"Sending command {command}")
        proc = await asyncio.create_subprocess_shell(
            " ".join(command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        print(f"Exit code {proc.returncode}")
        result_text = f"STDOUT: {stdout.decode()} STDERR: {stderr.decode()}"
        if proc.returncode is None:
            return (254, result_text)
        return (proc.returncode, result_text)

    async def _send_fax(
        self,
        destination: str,
        path: str,
        dest_name: Optional[str] = None,
        blocking: bool = False,
    ) -> bool:
        if self.host is None:
            raise Exception("Can not send fax without a host to fax from")
        host_strings = self._host_strings(destination)
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="dest", mode="w+t", delete=True
        ) as f:
            print(f"Wrote phone number {destination} to {f.name}")
            f.write(destination)
            f.flush()
            os.sync()
            time.sleep(1)
            uploaded_destination_file = await self._upload_file(f.name)
            if uploaded_destination_file is None:
                return False
            uploaded_path = await self._upload_file(path)
            if uploaded_path is None:
                return False
            command = ["sendfax", "-n", f"-B{self.max_speed}"]
            # For a blocking send cycle through the fax modems until one works
            if blocking:
                command.append("-w")
            for host_string in host_strings:
                command.extend(
                    [
                        f"-h{host_string}",
                        uploaded_path,
                        f"-z{uploaded_destination_file}",  # It is important this is last
                    ]
                )
                (exitcode, result_text) = await self._run_command(command)
                if exitcode != 0:
                    continue
                if not blocking:
                    return True
                else:
                    # Regex to match the job id
                    pattern = r"request id is (\d+)"

                    match = re.search(pattern, result_text, re.MULTILINE)

                    # Extracting the job ID
                    if match:
                        job_id = match.group(1)
                        print("Job ID:", job_id)
                        (exit_code, result_text) = await self._run_command(
                            ["faxstat", "-d"]
                        )
                        if exit_code != 0:
                            continue
                        # Regular expression pattern to check if the job ID succeeded
                        pattern = rf"^{job_id}\s+\d+\s+D\b"

                        match = re.search(pattern, result_text, re.MULTILINE)

                        # Checking if the job succeeded
                        if match:
                            print(f"Job ID {job_id} succeeded.")
                            return True
                        else:
                            print(f"Job ID {job_id} did not succeed.")
                            return False
                    else:
                        print("No job ID found.")
                        return False
            return False


class SshHylaFaxClient(HylaFaxClient):
    """HylaFaxClient that uses ssh to connect to a remote host, this is useful
    since the default ftp is... less than ideal (and does not do well with NAT)"""

    username = os.getenv("USERNAME", "idk")
    remote_host: Optional[str]

    def _create_ssh_client(self) -> SSHClient:
        if self.remote_host is None:
            raise Exception("Can not send fax without a host to fax from")
        from paramiko import SSHClient
        from paramiko.client import AutoAddPolicy

        ssh = SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(AutoAddPolicy)
        ssh.connect(self.remote_host)
        return ssh

    def _create_async_ssh_client(self):
        # Set known_hosts to none to skip ssh key check since we're all inside VPN
        return asyncssh.connect(self.remote_host, known_hosts=None)

    async def _upload_file(self, path: str) -> Optional[str]:
        ssh = self._create_ssh_client()
        target = f"/tmp/{self.username}/{path}"
        # Avoid //s
        if path[0] == "/":
            target = f"/tmp/{self.username}{path}"
        try:
            sftp_client = ssh.open_sftp()
            # Make the remote directory if needed.
            dir = os.path.dirname(target)
            (exit_code, _result_text) = await self._run_command(["mkdir", "-p", dir])
            if exit_code != 0:
                print("Failed to make dir")
                return None
            sftp_client.put(path, target)
            return target
        except Exception as e:
            print(
                f"Error during upload: {e} sending remote file {path} to {target} on {self.remote_host}"
            )
            return None

    async def _run_command(self, command: list[str]) -> Tuple[int, str]:
        try:
            async with self._create_async_ssh_client() as conn:
                print(f"Sending remote command {command}")
                process = conn.run(" ".join(command))
                result = await process
                print(f"Sent cmd")
                exit_code: int = result.exit_status
                result_text = f"STDOUT: {result.stdout} STDERR: {result.stderr}"
                if exit_code != 0:
                    print(f"Failed :( -- {result}")
                return (exit_code, result_text)
        except Exception as e:
            print(f"Error sending command {command} -- {e}")
            return (254, f"{e} fron {command}")


class FaxyMcFaxFace(SshHylaFaxClient):
    base_cost = 0
    cost_per_page = 1
    host = "localhost"
    remote_host = os.getenv("FAXYMCFAXFACE_HOST")
    max_speed = 9600
    default_modem = "ttyACM0"
    # IAX is... not working great right now
    # modem_mapping = {"1800": ["ttyIAX0"], "1844": ["ttyIAX0"], "1877": ["ttyIAX0"], "1888": ["ttyIAX0"]}


class FlexibleFaxMagic(object):
    """Fax interface that routes to different backends and chops pages etc."""

    def __init__(self, backends: list[FaxSenderBase], max_pages: int = 10):
        self.backends = backends
        self.max_pages = max_pages

    def _add_backend(self, backend: FaxSenderBase) -> None:
        self.backends.append(backend)

    async def _convert_input(self, input_path: str) -> Optional[str]:
        if input_path.endswith(".pdf"):
            return input_path
        else:
            await asyncio.sleep(0)
            base_convert_command = [
                "pandoc",
                "--wrap=auto",
                input_path,
                f"-o{input_path}.pdf",
            ]
            try:
                await check_call(base_convert_command)
                return f"{input_path}.pdf"
            # pandoc failures are often character encoding issues
            except Exception as e:
                # try to convert if we've got txt input
                new_input_path = input_path
                if input_path.endswith(".txt"):
                    try:
                        command = [
                            "iconv",
                            "-c",
                            "-t utf8",
                            f"-o{input_path}.u8.txt",
                            input_path,
                        ]
                        await check_call(command)
                        new_input_path = f"{input_path}.u8.txt"
                    except:
                        pass
                # Try a different engine
                for engine in ["lualatex", "xelatex"]:
                    convert_command = base_convert_command
                    convert_command.extend([f"--pdf-engine={engine}"])
                    try:
                        await check_call(base_convert_command)
                        return f"{input_path}.pdf"
                    except:
                        pass
                return None

    async def assemble_single_output(
        self, user_header: str, extra: str, input_paths: list[str]
    ) -> str:
        """Assembles all the inputs into one output. Will need to be chunked."""
        merger = PdfMerger()
        converted_paths = await asyncio.gather(
            *(self._convert_input(path) for path in input_paths)
        )

        for pdf_path in filter(None, converted_paths):
            merger.append(pdf_path)

        with tempfile.NamedTemporaryFile(
            suffix=".pdf", prefix="alltogether", mode="w+t", delete=False
        ) as t:
            merger.write(t.name)
            merger.close()
            return t.name

    def assemble_outputs(
        self, user_header: str, extra: str, input_paths: list[str]
    ) -> list[str]:
        """Assemble the outputs into chunks of max_pages length"""
        if len(input_paths) == 0:
            raise Exception(f"Empty fax request")
        # Keep track of the total pages
        total_input_pages = 0
        modified_paths = []
        # Start with converting everything into pdf & counting the pages
        for input_path in input_paths:
            reader = None
            pages = 0
            try:
                # Don't double convert pdfs
                if input_path.endswith(".pdf"):
                    reader = PdfReader(input_path)
                    pages = len(reader.pages)
                    modified_paths.append(input_path)
                else:
                    command = [
                        "pandoc",
                        "--wrap=auto",
                        input_path,
                        f"-o{input_path}.pdf",
                    ]
                    result = subprocess.run(command)
                    reader = PdfReader(f"{input_path}.pdf")
                    pages = len(reader.pages)
                    modified_paths.append(f"{input_path}.pdf")
                total_input_pages += pages
            except Exception as e:
                print(f"Skipping input {input_path} {e}")
        if len(modified_paths) == 0:
            raise Exception(f"Rejected all inputs from {input_paths}")
        # How many chunks do we need to make + 1
        number_of_transmissions = 1 + int(total_input_pages / self.max_pages)
        results: list[str] = []
        # Iterate through making the chunks
        input_index = 0
        current_input_file = PdfReader(modified_paths[0])
        index_in_current_file = 0
        for i in range(1, number_of_transmissions + 1):
            # Compute the number of pages in this transmission.
            x_pages = self.max_pages
            if i == number_of_transmissions:
                x_pages = total_input_pages % self.max_pages
            # Write out the header
            header_path = ""
            with tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="header", mode="w+t", delete=False
            ) as t:
                header = f"""This part of transmission {user_header} which is transmission {i} of {number_of_transmissions} with {x_pages}  pages in this transmission in addition to the cover page [this page]. {extra}"""
                t.write(header)
                t.flush()
                command = ["pandoc", t.name, f"-o{t.name}.pdf"]
                subprocess.run(command)
                header_path = f"{t.name}.pdf"
            with tempfile.NamedTemporaryFile(
                suffix=".pdf", prefix="combined", mode="w+t", delete=False
            ) as t:
                results.append(t.name)
                w = PdfWriter()
                header_reader = PdfReader(header_path)
                w.add_page(header_reader.pages[0])
                transmission_page_count = 0
                while transmission_page_count < self.max_pages:
                    if index_in_current_file < len(current_input_file.pages):
                        w.add_page(current_input_file.pages[index_in_current_file])
                        transmission_page_count += 1
                        index_in_current_file += 1
                    elif input_index + 1 < len(input_paths):
                        index_in_current_file = 0
                        input_index += 1
                        current_input_file = PdfReader(modified_paths[input_index])
                    else:
                        break
                w.write(t.name)
        return results

    async def send_fax(
        self, input_paths: list[str], extra: str, destination: str, blocking: bool
    ) -> bool:
        import uuid

        myuuid = uuid.uuid4()
        myuuidStr = str(myuuid)
        transmission_files = self.assemble_outputs(myuuidStr, extra, input_paths)
        for transmission in transmission_files:
            r = await self._send_fax(
                path=transmission, destination=destination, blocking=blocking
            )
            if r is False:
                return r
        time.sleep(1)
        # Clean the pdfs post transmission
        for f in transmission_files:
            os.remove(f)
        return True

    async def _send_fax(self, path: str, destination: str, blocking: bool) -> bool:
        page_count = len(PdfReader(path).pages)
        backends_by_cost = sorted(
            self.backends,
            key=lambda backend: backend.estimate_cost(destination, page_count),
        )
        for backend in backends_by_cost:
            print(f"Entering timeout ctx")
            with Timeout(600.0) as _timeout_ctx:
                try:
                    print(f"Calling backend {backend}")
                    r = await asyncio.wait_for(
                        backend.send_fax(
                            destination=destination, path=path, blocking=blocking
                        ),
                        timeout=300,
                    )
                    if r == True:
                        print(f"Sent fax to {destination} using {backend}")
                        return True
                except Exception as e:
                    print(f"Error {e} sending fax on {backend}")
        print(f"Unable to send fax to {destination} using {self.backends}")
        return False


flexible_fax_magic = FlexibleFaxMagic([FaxyMcFaxFace()])

# Add Sonic if we can support it
try:
    s = SonicFax()
    flexible_fax_magic._add_backend(s)
except:
    pass

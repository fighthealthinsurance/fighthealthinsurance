"""Load a staff letter review packet and assign its readers (letter_review.py).

The packet comes from the private eval repo. Read it from a file, or from
stdin with ``--file -`` so it can be piped in without the file ever landing
on the machine running the command.

Usage::

    python manage.py letter_review_import --file packet.json \\
        --assign reader_a=reader.a@example.com --assign reader_b=reader_b

    python manage.py letter_review_import --file - \\
        --assign reader_a=... --assign reader_b=... < packet.json

Every reader the packet names needs exactly one ``--assign`` to an active
staff account, by username or email. The packet is checked strictly before
anything is written (exact fields, unique keys, every item reader a packet
reader, sane lengths) and the write is one transaction, so a refused import
leaves nothing behind. A packet name that already exists is refused unless
``--replace``, which deletes the old packet and its labels: export them first.

Output names the packet, handles, accounts and counts. It never prints a
letter or a prompt.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.letter_review import (
    PacketError,
    import_packet,
    parse_assignments,
    parse_packet,
    resolve_readers,
)


def _no_repeated_fields(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """json's object hook: a field given twice is refused, not last-wins."""
    seen: Dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise PacketError(f"a JSON object gives the field {name!r} twice")
        seen[name] = value
    return seen


class Command(BaseCommand):
    help = (
        "Load a letter review packet (JSON file or '-' for stdin) and assign "
        "each of its readers to a staff account."
    )
    # Lets tests hand the command a stdin of their own.
    stealth_options = ("stdin",)

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--file",
            "-f",
            required=True,
            help="Path to the packet JSON, or '-' to read it from stdin.",
        )
        parser.add_argument(
            "--assign",
            action="append",
            default=[],
            metavar="HANDLE=EMAIL_OR_USERNAME",
            help=(
                "Tie a packet reader handle to an active staff account. "
                "Give one per reader."
            ),
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "Overwrite a packet of the same name. Its labels are deleted, "
                "so export them first."
            ),
        )

    def _load(self, source: str, stdin: Any) -> Any:
        try:
            if source == "-":
                return json.load(stdin, object_pairs_hook=_no_repeated_fields)
            path = Path(source)
            if not path.is_file():
                raise CommandError(f"No packet file at {source}")
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh, object_pairs_hook=_no_repeated_fields)
        except json.JSONDecodeError as exc:
            # The reason and the position only, never the text around it.
            raise CommandError(
                f"The packet is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from None
        except UnicodeDecodeError:
            raise CommandError("The packet is not UTF-8 text") from None
        except PacketError as exc:
            raise CommandError(str(exc)) from None

    def handle(self, *args: Any, **options: Any) -> None:
        source: str = options["file"]
        data = self._load(source, options.get("stdin") or sys.stdin)
        try:
            packet = parse_packet(data)
            users = resolve_readers(packet, parse_assignments(options["assign"]))
            result = import_packet(packet, users, replace=options["replace"])
        except PacketError as exc:
            raise CommandError(f"Packet refused: {exc}") from None

        shared = sum(1 for item in packet.items if len(item.readers) > 1)
        verb = "Replaced" if result.replaced else "Imported"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} letter review packet {packet.name!r} "
                f"(rule {packet.rule_version}): {len(packet.items)} items, "
                f"{shared} read by more than one reader"
            )
        )
        for handle in packet.readers:
            self.stdout.write(
                f"  {handle} -> {users[handle].get_username()}: "
                f"{result.items_per_reader[handle]} items"
            )
        if result.discarded_labels:
            self.stderr.write(
                self.style.WARNING(
                    f"The replaced packet's {result.discarded_labels} labels "
                    "were deleted with it"
                )
            )

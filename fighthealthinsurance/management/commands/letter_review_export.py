"""Write a staff letter review packet's labels as JSON (letter_review.py).

Usage::

    python manage.py letter_review_export --packet NAME            # to stdout
    python manage.py letter_review_export --packet NAME --out labels.json

The JSON is the labels contract the private eval repo reads: the packet
name, its rule version, when it was exported, and one entry per label with
the item key, reader handle, verdict, note and when it was labeled. Unlabeled
items are left out. No letter or prompt text is in it. With ``--out -`` (the
default) stdout carries only the JSON and the summary goes to stderr.
"""

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.letter_review import export_labels
from fighthealthinsurance.models import LetterReviewPacket


class Command(BaseCommand):
    help = "Write a letter review packet's labels JSON to a file or stdout."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--packet", required=True, help="The packet name.")
        parser.add_argument(
            "--out",
            "-o",
            default="-",
            help="Where to write the JSON, or '-' (the default) for stdout.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        name: str = options["packet"]
        out: str = options["out"]
        packet = LetterReviewPacket.objects.filter(name=name).first()
        if packet is None:
            raise CommandError(f"No letter review packet named {name!r}")
        data = export_labels(packet)
        text = json.dumps(data, indent=2) + "\n"
        summary = f"Exported {len(data['labels'])} labels from packet {name!r}"
        if out == "-":
            self.stdout.write(text, ending="")
            self.stderr.write(summary)
            return
        try:
            Path(out).write_text(text, encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Could not write {out}: {exc.strerror}") from None
        self.stdout.write(self.style.SUCCESS(f"{summary} to {out}"))

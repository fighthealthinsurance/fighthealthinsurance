"""Delete a staff letter review packet and everything under it (letter_review.py).

Usage::

    python manage.py letter_review_delete --packet NAME --yes

Removes the packet, its readers, its items (letters and prompts) and every
label, in one go. Export the labels first if they are wanted. Without
``--yes`` it only says what it would delete. It prints counts, never a letter
or a prompt.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fighthealthinsurance.models import (
    LetterReviewItem,
    LetterReviewLabel,
    LetterReviewPacket,
    LetterReviewReader,
)


class Command(BaseCommand):
    help = "Delete a letter review packet with its readers, letters and labels."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--packet", required=True, help="The packet name.")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Really delete. Without it nothing is deleted.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        name: str = options["packet"]
        with transaction.atomic():
            packet = (
                LetterReviewPacket.objects.select_for_update().filter(name=name).first()
            )
            if packet is None:
                raise CommandError(f"No letter review packet named {name!r}")
            items = LetterReviewItem.objects.filter(packet=packet).count()
            readers = LetterReviewReader.objects.filter(packet=packet).count()
            labels = LetterReviewLabel.objects.filter(item__packet=packet).count()
            counts = f"{items} items, {readers} readers and {labels} labels"
            if not options["yes"]:
                raise CommandError(
                    f"Not deleted: pass --yes to delete packet {name!r} "
                    f"with its {counts}"
                )
            packet.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted letter review packet {name!r}: {counts}")
        )

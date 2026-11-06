"""Send shared plan documents back to be read again, after a reading that
saw ciphertext instead of the document.

Until combined_storage.correct_size, the bucket kept each encrypted file
cut short, nothing decrypted, and the reading stored the ciphertext as the
document's text or found it unreadable. read_and_decrypt_file now salvages
what was kept, so these rows read again get the part of the document that
survived. Lists what it would change unless ``--apply``::

    python manage.py reread_shared_documents
    python manage.py reread_shared_documents --apply

Applied, the rows go back to waiting with their text cleared, and the
hourly sweep reads them. Safe to run again: a row read since is left alone.
"""

from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.ml.ml_document_extraction import looks_encrypted
from fighthealthinsurance.models import PolicyDocument


class Command(BaseCommand):
    help = "Re-read shared plan documents whose reading saw ciphertext."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--apply", action="store_true", help="Change the rows (default: list)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        rows = PolicyDocument.objects.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            processing_status__in=(
                PolicyDocument.PROCESSING_READY,
                PolicyDocument.PROCESSING_UNREADABLE,
            ),
        ).only("pk", "processing_status", "cleaned_text")
        damaged = [
            row.pk
            for row in rows
            if row.processing_status == PolicyDocument.PROCESSING_UNREADABLE
            or looks_encrypted((row.cleaned_text or "").encode())
        ]
        self.stdout.write(f"{len(damaged)} shared file(s) to read again")
        if not options["apply"] or not damaged:
            return
        updated = PolicyDocument.objects.filter(
            pk__in=damaged,
            processing_status__in=(
                PolicyDocument.PROCESSING_READY,
                PolicyDocument.PROCESSING_UNREADABLE,
            ),
        ).update(
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            processed_at=None,
            cleaned_text="",
        )
        self.stdout.write(f"{updated} sent back; the hourly sweep reads them")

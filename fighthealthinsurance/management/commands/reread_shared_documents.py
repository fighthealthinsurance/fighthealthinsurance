"""Send shared plan documents back to be read again: unreadable ones, and
ones stored as ready whose text is ciphertext or garbage.

Until combined_storage.correct_size, the bucket kept each encrypted file
cut short, nothing decrypted, and the reading stored the ciphertext as the
document's text or found it unreadable. read_and_decrypt_file now salvages
what was kept, so these rows read again get the part of the document that
survived. Scans, and PDFs whose fonts give garbage text, are now read with
OCR, so unreadable rows and rows whose stored text is garbled
(text_quality.is_garbled) read again may get their text. Lists what it
would change unless ``--apply``; it never prints a row's text. Rows whose
reading notes that OCR could not run or did not finish go back too::

    python manage.py reread_shared_documents
    python manage.py reread_shared_documents --apply

Applied, the rows go back to waiting with their text cleared, and the
hourly sweep reads them. A row read again between the list and the change
keeps its new reading. Run again, it sends back the files that are still
unreadable, which costs one more reading each and nothing else.
"""

import re
from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.ml.ml_document_extraction import looks_encrypted
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.text_quality import is_garbled

# The page markers a PDF's reading puts in, which aren't the document's words.
_PAGE_MARKERS = re.compile(r"^\[Page \d+\]$", re.MULTILINE)
# The note a reading leaves when OCR could not run or did not finish: read
# again, those pages may now be read.
_OCR_FAILED = re.compile(
    r"pages could not be read: OCR (could not run|did not finish)\.\]"
)


def stored_text_is_damaged(cleaned_text: str) -> bool:
    """Ciphertext, garbage once the page markers are taken out, or a reading
    OCR could not finish."""
    if looks_encrypted(cleaned_text[:64].encode()):
        return True
    if _OCR_FAILED.search(cleaned_text):
        return True
    return is_garbled(_PAGE_MARKERS.sub("", cleaned_text))


class Command(BaseCommand):
    help = "Re-read shared plan documents that are unreadable or stored as garbage."

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
        ).only("pk", "processing_status", "processed_at", "cleaned_text")
        # One row at a time, keeping only what the change needs: a reading
        # can run to millions of characters.
        damaged = [
            (row.pk, row.processing_status, row.processed_at)
            for row in rows.iterator(chunk_size=50)
            if row.processing_status == PolicyDocument.PROCESSING_UNREADABLE
            or stored_text_is_damaged(row.cleaned_text or "")
        ]
        garbled = sum(
            1 for _, status, _ in damaged if status == PolicyDocument.PROCESSING_READY
        )
        self.stdout.write(
            f"{len(damaged)} shared file(s) to read again: "
            f"{len(damaged) - garbled} unreadable, {garbled} stored as ready "
            "with ciphertext, garbled text or pages OCR could not read"
        )
        if not options["apply"] or not damaged:
            return
        updated = 0
        for pk, status, processed_at in damaged:
            # Only a row still as it was listed: one read again since keeps
            # its new reading.
            updated += PolicyDocument.objects.filter(
                pk=pk, processing_status=status, processed_at=processed_at
            ).update(
                processing_status=PolicyDocument.PROCESSING_RECEIVED,
                processed_at=None,
                cleaned_text="",
            )
        self.stdout.write(f"{updated} sent back; the hourly sweep reads them")

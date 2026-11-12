"""Check now that each shared plan document still has its file, whole, in
the object store (shared_file_check), as the nightly sweep does. Prints the
counts by verdict; reads stored bytes, decrypts nothing, changes no row and
keeps nothing::

    python manage.py check_shared_files
"""

from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.shared_file_check import check_shared_files


class Command(BaseCommand):
    help = (
        "Count shared plan documents whose stored file is whole, cut short or missing."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        for name, count in check_shared_files().items():
            self.stdout.write(f"{name[len('file_check_'):].replace('_', ' ')}: {count}")

"""Link shared plan documents already read to the companies they name.

The reading links each new document to its company (plan_sponsors); this
does the same for documents read before that, from their cleaned text. Lists
what it would link unless ``--apply``::

    python manage.py link_plan_sponsors
    python manage.py link_plan_sponsors --apply

Only rows read and ready for review with no company yet are looked at, so a
company staff set is never changed. Prints counts and company names only.
"""

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_sponsors import identify_sponsor, link_sponsor


class Command(BaseCommand):
    help = "Link shared plan documents already read to the companies they name."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--apply", action="store_true", help="Link them (default: list)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        rows = PolicyDocument.objects.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            processing_status=PolicyDocument.PROCESSING_READY,
            plan_sponsor__isnull=True,
        ).only("pk", "cleaned_text")
        found: Counter[str] = Counter()
        looked = 0
        for row in rows.iterator(chunk_size=50):
            looked += 1
            sponsor = identify_sponsor(row.cleaned_text or "")
            if sponsor is None:
                continue
            if options["apply"]:
                company = link_sponsor(row.pk, sponsor)
                found[company.name] += 1
            else:
                found[sponsor.name] += 1
        verb = "linked" if options["apply"] else "would be linked"
        self.stdout.write(
            f"{looked} document(s) looked at, {sum(found.values())} {verb} "
            f"to {len(found)} compan{'y' if len(found) == 1 else 'ies'}"
        )
        for name, count in sorted(found.items()):
            self.stdout.write(f"  {name}: {count}")

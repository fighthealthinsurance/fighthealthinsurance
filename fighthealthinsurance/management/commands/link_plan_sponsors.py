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
from fighthealthinsurance.plan_sponsors import (
    identify_sponsor,
    link_sponsor,
    match_sponsor,
    normalize_name,
)


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
        # Each company once, by the same matching the linking uses
        # (match_sponsor): by its EIN, then its name.
        found: Counter[tuple[str, str]] = Counter()
        names: dict[tuple[str, str], str] = {}
        looked = 0
        for row in rows.iterator(chunk_size=50):
            looked += 1
            sponsor = identify_sponsor(row.cleaned_text or "")
            if sponsor is None:
                continue
            if options["apply"]:
                company = link_sponsor(row.pk, sponsor)
                key, name = ("on the list", str(company.pk)), company.name
            else:
                company = match_sponsor(sponsor)
                if company is not None:
                    key, name = ("on the list", str(company.pk)), company.name
                elif sponsor.ein:
                    key, name = ("new", sponsor.ein), f"{sponsor.name} (new)"
                else:
                    key = ("new", normalize_name(sponsor.name))
                    name = f"{sponsor.name} (new)"
            found[key] += 1
            names.setdefault(key, name)
        verb = "linked" if options["apply"] else "would be linked"
        self.stdout.write(
            f"{looked} document(s) looked at, {sum(found.values())} {verb} "
            f"to {len(found)} compan{'y' if len(found) == 1 else 'ies'}"
        )
        for key, count in sorted(found.items(), key=lambda item: names[item[0]]):
            self.stdout.write(f"  {names[key]}: {count}")

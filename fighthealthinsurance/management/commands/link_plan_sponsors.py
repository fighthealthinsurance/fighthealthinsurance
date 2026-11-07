"""Link shared plan documents already read to the companies they name.

The reading links each new document to its company: our model's reading,
checked against the text, or the fixed patterns when no model answers
(plan_documents_processing.afind_company). This does the same for documents
read before that, from their cleaned text. Lists what it would link unless
``--apply``::

    python manage.py link_plan_sponsors
    python manage.py link_plan_sponsors --apply

Only rows read and ready for review with no company yet are looked at, so a
company staff set is never changed. Prints counts and company names only.
"""

from collections import Counter
from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_documents_processing import afind_company
from fighthealthinsurance.plan_sponsors import (
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
        )
        # Each company once, by the same matching the linking uses
        # (match_sponsor): by its EIN, then its name.
        found: Counter[tuple[str, str]] = Counter()
        names: dict[tuple[str, str], str] = {}
        sources: Counter[str] = Counter()
        looked = 0
        # Read the ids first: the model's reading takes seconds a document,
        # longer than a database cursor should stay open.
        for pk, text in list(rows.values_list("pk", "cleaned_text")):
            looked += 1
            sponsor = async_to_sync(afind_company)(text or "")
            if sponsor is None:
                continue
            sources[sponsor.source] += 1
            if options["apply"]:
                linked = link_sponsor(pk, sponsor)
                key, name = ("on the list", str(linked.pk)), linked.name
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
        if sources:
            self.stdout.write(
                "  found by: "
                + ", ".join(f"{s} {n}" for s, n in sorted(sources.items()))
            )
        for key, count in sorted(found.items(), key=lambda item: names[item[0]]):
            self.stdout.write(f"  {names[key]}: {count}")

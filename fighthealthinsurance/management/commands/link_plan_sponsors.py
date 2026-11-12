"""Link shared plan documents already read to the companies they name.

The reading links each new document to its company: our model's reading,
checked against the text, or the fixed patterns when no model answers
(plan_documents_processing.afind_company). This does the same for documents
read before that, from their cleaned text. Lists what it would link unless
``--apply``::

    python manage.py link_plan_sponsors
    python manage.py link_plan_sponsors --apply

Only rows read and ready for review with no company yet are looked at, so a
company staff set is never changed. Rows whose lookup failed during their
reading (company_lookup_failures) are among them, and counted. Prints
counts and company names only.
"""

from collections import Counter
from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand
from django.db.models import F

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_documents_processing import afind_company, ready_row
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
        failed_before = rows.filter(company_lookup_failures__gt=0).count()
        failed_now = 0
        # The ids first, then each text on its own: the model's reading
        # takes seconds a document, longer than a database cursor should
        # stay open, and the texts together can be large.
        for pk in list(rows.values_list("pk", flat=True)):
            text = rows.filter(pk=pk).values_list("cleaned_text", flat=True).first()
            if text is None:
                continue
            looked += 1
            try:
                sponsor = async_to_sync(afind_company)(text)
                linked = (
                    link_sponsor(pk, sponsor)
                    if options["apply"] and sponsor is not None
                    else None
                )
            except Exception as e:
                # Counted like a failure in a reading; the next row goes on.
                failed_now += 1
                self.stderr.write(f"  a lookup failed: {type(e).__name__}")
                if options["apply"]:
                    ready_row(pk).update(
                        company_lookup_failures=F("company_lookup_failures") + 1
                    )
                continue
            if options["apply"]:
                # This lookup finished, so earlier failures no longer stand.
                ready_row(pk).filter(company_lookup_failures__gt=0).update(
                    company_lookup_failures=0
                )
            if sponsor is None:
                continue
            if linked is not None:
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
            sources[sponsor.source] += 1
            found[key] += 1
            names.setdefault(key, name)
        verb = "linked" if options["apply"] else "would be linked"
        self.stdout.write(
            f"{looked} document(s) looked at, {sum(found.values())} {verb} "
            f"to {len(found)} compan{'y' if len(found) == 1 else 'ies'}"
        )
        self.stdout.write(
            f"  {failed_before} of them had a company lookup fail before; "
            f"{failed_now} failed this time"
        )
        if sources:
            self.stdout.write(
                "  found by: "
                + ", ".join(f"{s} {n}" for s, n in sorted(sources.items()))
            )
        for key, count in sorted(found.items(), key=lambda item: names[item[0]]):
            self.stdout.write(f"  {names[key]}: {count}")

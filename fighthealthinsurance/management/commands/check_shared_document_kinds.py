"""Check shared plan documents already read for whether each is a plan
(plan_document_kind), as new readings now are.

A shared file read as ready before the check came in may be a person's own
papers (a bill, an EOB, a letter about them) or have no health plan words.
This judges each such row's stored, cleaned text. It counts them unless
``--apply``, and it never prints a row's text::

    python manage.py check_shared_document_kinds
    python manage.py check_shared_document_kinds --apply

Rows whose stored text is damaged or garbled are left alone for
reread_shared_documents. The count says how many personal verdicts rest
on two kinds of a person's details alone, the rule that is easiest to
get wrong.

Applied, a person's own papers lose their stored text, and the line
quoted for their company, and are marked personal, their encrypted file
kept; a file with no plan words is marked
not a plan, its text kept. Neither is counted as a plan. Rows staff said
are plans are left alone, and so is a row read again between the count and
the change.
"""

from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.management.commands.reread_shared_documents import (
    stored_text_is_damaged,
)
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_document_kind import BY_DETAILS, OTHER, PERSONAL, judge

_STATUS_FOR = {
    PERSONAL: PolicyDocument.PROCESSING_PERSONAL,
    OTHER: PolicyDocument.PROCESSING_NOT_A_PLAN,
}


class Command(BaseCommand):
    help = "Mark shared plan documents already read as personal papers or not a plan."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--apply", action="store_true", help="Change the rows (default: count)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        rows = PolicyDocument.objects.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            processing_status=PolicyDocument.PROCESSING_READY,
            staff_says_plan=False,
        ).only("pk", "processed_at", "cleaned_text")
        # One row at a time, keeping only what the change needs: a reading
        # can run to millions of characters.
        found: dict[str, list[tuple[Any, Any]]] = {PERSONAL: [], OTHER: []}
        by_details_alone = damaged = 0
        for row in rows.iterator(chunk_size=50):
            text = row.cleaned_text or ""
            if stored_text_is_damaged(text):
                # Garbage is never judged: reread_shared_documents reads it again.
                damaged += 1
                continue
            kind, rule = judge(text)
            if kind in found:
                found[kind].append((row.pk, row.processed_at))
                by_details_alone += kind == PERSONAL and rule == BY_DETAILS
        self.stdout.write(
            f"{len(found[PERSONAL])} shared file(s) read as ready would become "
            f"personal (text cleared, file kept); {by_details_alone} of them only "
            "because they carry two kinds of a person's details"
        )
        self.stdout.write(
            f"{len(found[OTHER])} shared file(s) read as ready would become "
            "not a plan (text kept)"
        )
        self.stdout.write(
            f"{damaged} shared file(s) read as ready have damaged or garbled "
            "text: left for reread_shared_documents"
        )
        if not options["apply"]:
            return
        for kind, listed in found.items():
            changes: dict[str, Any] = {"processing_status": _STATUS_FOR[kind]}
            if kind == PERSONAL:
                # The company's quoted line is the document's text too.
                changes["cleaned_text"] = ""
                changes["plan_sponsor_evidence"] = ""
            updated = 0
            for pk, processed_at in listed:
                # Only a row still as it was counted.
                updated += PolicyDocument.objects.filter(
                    pk=pk,
                    processing_status=PolicyDocument.PROCESSING_READY,
                    processed_at=processed_at,
                    staff_says_plan=False,
                ).update(**changes)
            self.stdout.write(f"{updated} marked {_STATUS_FOR[kind]}")

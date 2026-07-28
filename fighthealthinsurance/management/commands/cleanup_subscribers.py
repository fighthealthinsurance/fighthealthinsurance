"""Management command to find (and optionally delete) junk mailing list subscribers.

Reports by default -- nothing is deleted without ``--apply``. See
``fighthealthinsurance/subscriber_hygiene.py`` for what each reason code means.

Examples::

    # What's wrong with the list?
    python manage.py cleanup_subscribers

    # ...including the other inbound-contact tables (report only, never deleted)
    python manage.py cleanup_subscribers --all-sources

    # Delete the unambiguous junk (unmailable, invalid, duplicate, spam markup)
    python manage.py cleanup_subscribers --apply

    # Delete only exact duplicates
    python manage.py cleanup_subscribers --apply --reasons duplicate_email
"""

from typing import Any, List

from django.core.management.base import BaseCommand, CommandError, CommandParser

from fighthealthinsurance import subscriber_hygiene as hygiene
from fighthealthinsurance.utils import mask_email_for_logging


class Command(BaseCommand):
    help = (
        "Report on suspicious/junk mailing list subscribers and, with --apply, "
        "delete them."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete the matching subscribers (default: report only)",
        )
        parser.add_argument(
            "--reasons",
            type=str,
            default="",
            help=(
                "Comma-separated reason codes to act on. Defaults to every "
                "auto-cleanable reason: "
                + ", ".join(sorted(hygiene.AUTO_CLEANABLE_CODES))
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Allow --apply with review-only reasons (spam TLDs, role "
                "accounts, alias duplicates, ...). Use with care."
            ),
        )
        parser.add_argument(
            "--include-reviewed",
            action="store_true",
            help="Also consider rows staff already reviewed and chose to keep",
        )
        parser.add_argument(
            "--all-sources",
            action="store_true",
            help=(
                "Also report on chat leads, demo requests and interested "
                "professionals (report only -- these are never deleted here)"
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="How many example rows to print per reason (default: 10)",
        )

    def handle(self, *args: str, **options: Any) -> None:
        apply_changes: bool = options["apply"]
        force: bool = options["force"]
        limit: int = options["limit"]
        requested = [
            code.strip() for code in options["reasons"].split(",") if code.strip()
        ]

        unknown = [code for code in requested if code not in hygiene.REASONS]
        if unknown:
            raise CommandError(
                f"Unknown reason code(s): {', '.join(unknown)}. "
                f"Known codes: {', '.join(sorted(hygiene.REASONS))}"
            )

        codes = set(requested) if requested else set(hygiene.AUTO_CLEANABLE_CODES)
        risky = sorted(codes - hygiene.AUTO_CLEANABLE_CODES)
        if apply_changes and risky and not force:
            raise CommandError(
                f"{', '.join(risky)} are review-only reasons -- a human should "
                "look at those rows (staff page: /timbit/help/subscriber_cleanup). "
                "Pass --force to delete on them anyway."
            )

        result = hygiene.scan_subscribers(
            include_reviewed=options["include_reviewed"],
        )
        self._report(result, limit=limit)

        if options["all_sources"]:
            for other in hygiene.scan_other_contact_tables():
                self._report(other, limit=limit)

        targets: List[int] = [
            finding.record.pk
            for finding in result.findings
            if codes.intersection(finding.codes)
        ]

        if not targets:
            self.stdout.write(self.style.SUCCESS("Nothing to clean up."))
            return

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(targets)} subscriber(s) match {', '.join(sorted(codes))}. "
                    "Re-run with --apply to delete them."
                )
            )
            return

        deleted = hygiene.delete_subscribers(targets, actor="cleanup_subscribers")
        self.stdout.write(self.style.SUCCESS(f"\nDeleted {deleted} subscriber row(s)."))

    def _report(self, result: hygiene.ScanResult, limit: int) -> None:
        self.stdout.write(
            f"\n=== {result.source}: {result.flagged_count} flagged of "
            f"{result.scanned} scanned ==="
        )
        if not result.findings:
            self.stdout.write("  (clean)")
            return

        counts = result.counts_by_code()
        for code, count in counts.items():
            reason = hygiene.REASONS[code]
            bucket = "auto-cleanable" if reason.auto_cleanable else "review only"
            self.stdout.write(f"\n  {code}: {count} ({bucket}) -- {reason.description}")
            for finding in result.findings_with_code(code)[:limit]:
                record = finding.record
                # Addresses are masked: this output lands in terminals and logs.
                self.stdout.write(
                    f"    #{record.pk} {mask_email_for_logging(record.email)}"
                    f" [{finding.details.get(code, '')}]"
                )
            if count > limit:
                self.stdout.write(f"    ... and {count - limit} more")

        auto = len(result.auto_cleanable_findings)
        self.stdout.write(
            f"\n  {auto} row(s) carry at least one auto-cleanable reason; "
            f"{result.flagged_count - auto} need human review."
        )

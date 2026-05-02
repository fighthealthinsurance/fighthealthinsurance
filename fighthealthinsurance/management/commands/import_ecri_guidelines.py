"""
Management command to import / upsert clinical practice guideline summaries
from the ECRI Guidelines Trust into the ``ECRIGuideline`` table.

The command reads a JSON file (or stdin) with a list of guideline records.
Each record must include at minimum a ``guideline_id`` and ``title``; all
other fields are optional and default to empty/None.

Example:

    python manage.py import_ecri_guidelines \\
        --source fighthealthinsurance/fixtures/ecri_guidelines_seed.json

Records with an existing ``guideline_id`` are updated in place. The
``--deactivate-missing`` flag will mark any guideline whose ``guideline_id``
does not appear in the input file as inactive (``is_active=False``) without
deleting it.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fighthealthinsurance.models import ECRIGuideline

_DATE_KEYS = ("publication_date", "last_updated")
_TEXT_KEYS = (
    "title",
    "developer_organization",
    "recommendations_summary",
    "intended_population",
    "intended_users",
    "evidence_quality",
    "url",
    "source",
)
_LIST_KEYS = ("procedure_keywords", "diagnosis_keywords", "topics")


def _parse_date(value: Any) -> Optional[date]:
    """Parse YYYY-MM-DD or YYYY date strings; return None for falsy values."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw input record into kwargs for ECRIGuideline upsert."""
    fields: Dict[str, Any] = {}
    for key in _TEXT_KEYS:
        if key in raw and raw[key] is not None:
            fields[key] = str(raw[key]).strip()
    for key in _DATE_KEYS:
        if key in raw:
            parsed = _parse_date(raw[key])
            if parsed is not None:
                fields[key] = parsed
    for key in _LIST_KEYS:
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise CommandError(
                f"Field {key!r} must be a JSON array (got {type(value).__name__})"
            )
        # Lower-case + dedupe while preserving order so matching is consistent.
        seen: List[str] = []
        for item in value:
            token = str(item).strip().lower()
            if token and token not in seen:
                seen.append(token)
        fields[key] = seen
    if "is_active" in raw:
        fields["is_active"] = bool(raw["is_active"])
    return fields


class Command(BaseCommand):
    help = (
        "Import or update ECRI Guidelines Trust clinical practice guideline "
        "summaries from a JSON file."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--source",
            "-s",
            default="-",
            help=(
                "Path to a JSON file with a list of guideline records. "
                "Use '-' (the default) to read from stdin."
            ),
        )
        parser.add_argument(
            "--deactivate-missing",
            action="store_true",
            help=(
                "Mark any existing ECRIGuideline whose guideline_id is not "
                "present in the input as is_active=False (rather than deleting)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate the input but do not write to the database.",
        )

    def _load_records(self, source: str) -> List[Dict[str, Any]]:
        if source == "-":
            data = json.load(sys.stdin)
        else:
            path = Path(source)
            if not path.exists():
                raise CommandError(f"Source file does not exist: {source}")
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        if not isinstance(data, list):
            raise CommandError(
                "Input must be a JSON array of guideline objects "
                f"(got {type(data).__name__})"
            )
        return data

    def handle(self, *args: str, **options: Any) -> None:
        source: str = options["source"]
        deactivate_missing: bool = options["deactivate_missing"]
        dry_run: bool = options["dry_run"]

        records = self._load_records(source)
        self.stdout.write(f"Loaded {len(records)} record(s) from {source!r}")

        seen_ids: List[str] = []
        created = 0
        updated = 0
        skipped = 0

        with transaction.atomic():
            for raw in records:
                guideline_id = (raw.get("guideline_id") or "").strip()
                title = (raw.get("title") or "").strip()
                if not guideline_id or not title:
                    self.stderr.write(
                        self.style.WARNING(
                            f"Skipping record without guideline_id/title: {raw!r}"
                        )
                    )
                    skipped += 1
                    continue

                seen_ids.append(guideline_id)
                fields = _normalize_record(raw)
                fields.setdefault("title", title)

                if dry_run:
                    continue

                obj, was_created = ECRIGuideline.objects.update_or_create(
                    guideline_id=guideline_id,
                    defaults=fields,
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            deactivated = 0
            if deactivate_missing and not dry_run and seen_ids:
                deactivated = ECRIGuideline.objects.exclude(
                    guideline_id__in=seen_ids
                ).update(is_active=False)

            if dry_run:
                # Roll back any incidental work just to be safe.
                transaction.set_rollback(True)

        msg = (
            f"created={created} updated={updated} skipped={skipped} "
            f"deactivated={deactivated if deactivate_missing else 'n/a'} "
            f"dry_run={dry_run}"
        )
        self.stdout.write(self.style.SUCCESS(f"ECRI guideline import done: {msg}"))

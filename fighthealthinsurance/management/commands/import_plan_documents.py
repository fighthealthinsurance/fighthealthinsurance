"""Load a plan-document bundle into PublicPlanDocument (plan_document_bundle).

The bundle comes from the plan-document pipeline in
healthinsurance-llm-internal. Read it from a file, or from stdin with
``--source -`` so it can be piped into a pod without landing on disk::

    python manage.py import_plan_documents --source bundle.json --dry-run
    kubectl exec -i <pod> -- python manage.py import_plan_documents --source - < bundle.json

A record that names a ``file`` needs ``--files-root`` (so a piped bundle
carries its text inline, as ``pages`` or ``text``). Records are checked one
by one: a refused record is skipped and counted, and the run exits 0 with
the summary styled as a warning; ``--strict`` refuses the whole bundle on
the first bad record instead. A bundle that cannot be read at all (not
JSON, too large, a key given twice) is a CommandError. ``--dry-run`` does
everything and rolls it back; ``--deactivate-missing`` retires the named
carriers' documents the bundle no longer lists.

Output names keys, fields and counts. It never prints a document's text.
"""

import datetime
import json
import sys
from pathlib import Path
from typing import Any, List, Union

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.plan_document_bundle import (
    MAX_BUNDLE_BYTES,
    BundleError,
    import_bundle,
    no_repeated_fields,
    parse_bundle,
    parse_jsonl,
)

READ_CHUNK = 1 << 20  # 1 MiB


class Command(BaseCommand):
    help = "Load a plan-document bundle (JSON or JSONL; a file or '-' for stdin)."
    # Lets tests hand the command a stdin of their own.
    stealth_options = ("stdin",)

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--source",
            "-s",
            default="-",
            help="Path to the bundle, or '-' (the default) to read it from stdin.",
        )
        parser.add_argument(
            "--format",
            choices=("auto", "json", "jsonl"),
            default="auto",
            help="auto reads a leading '[' or '{' as JSON and anything else as JSON Lines.",
        )
        parser.add_argument(
            "--files-root",
            default=None,
            help="Directory the records' relative 'file' paths are read under.",
        )
        parser.add_argument(
            "--store-file",
            action="store_true",
            help="Keep the original of each 'file' record in storage.",
        )
        parser.add_argument(
            "--batch",
            default=None,
            help="Label stored on every row this run writes (default: a UTC timestamp).",
        )
        parser.add_argument(
            "--deactivate-missing",
            action="store_true",
            help=(
                "Mark the named carriers' documents that the bundle does not list "
                "as inactive. Only those carriers' rows are touched."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and resolve everything, then roll back.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Refuse the whole bundle on the first bad record or warning.",
        )

    def _read(self, stream: Any) -> Union[bytes, str]:
        """The stream's contents, refused once they pass MAX_BUNDLE_BYTES,
        read a chunk at a time."""
        chunks: List[Any] = []
        size = 0
        while size <= MAX_BUNDLE_BYTES:
            chunk = stream.read(min(READ_CHUNK, MAX_BUNDLE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_BUNDLE_BYTES:
            raise CommandError(f"The bundle is larger than {MAX_BUNDLE_BYTES} bytes")
        if chunks and isinstance(chunks[0], bytes):
            return b"".join(chunks)
        return "".join(chunks)

    def _load(self, source: str, fmt: str, stdin: Any) -> List[dict]:
        try:
            if source == "-":
                raw = self._read(getattr(stdin, "buffer", stdin))
            else:
                path = Path(source)
                if not path.is_file():
                    raise CommandError(f"No bundle file at {source}")
                with path.open("rb") as fh:
                    raw = self._read(fh)
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeDecodeError:
            raise CommandError("The bundle is not UTF-8 text") from None
        if fmt == "auto":
            if source.lower().endswith(".jsonl"):
                fmt = "jsonl"
            else:
                head = text.lstrip()[:1]
                fmt = "json" if head in ("[", "{") else "jsonl"
        try:
            if fmt == "jsonl":
                return parse_jsonl(text.splitlines())
            try:
                data = json.loads(text, object_pairs_hook=no_repeated_fields)
            except json.JSONDecodeError as exc:
                raise CommandError(
                    f"The bundle is not valid JSON: {exc.msg} "
                    f"(line {exc.lineno}, column {exc.colno})"
                ) from None
            except RecursionError:
                raise CommandError(
                    "The bundle is not valid JSON: it nests too deeply"
                ) from None
            except ValueError:
                raise CommandError(
                    "The bundle is not valid JSON: it has a number too long to read"
                ) from None
            return parse_bundle(data)
        except BundleError as exc:
            raise CommandError(f"Bundle refused: {exc}") from None

    def handle(self, *args: Any, **options: Any) -> None:
        records = self._load(
            options["source"], options["format"], options.get("stdin") or sys.stdin
        )
        batch = options["batch"] or datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        self.stdout.write(f"Loaded {len(records)} record(s) from {options['source']!r}")
        if options["files_root"] is None:
            # A missing flag is the operator's mistake, not a bad record: a
            # piped bundle carries its text inline (pages or text).
            with_files = [
                str(record.get("document_key") or index)
                for index, record in enumerate(records)
                if record.get("file")
            ]
            if with_files:
                raise CommandError(
                    f"{len(with_files)} record(s) name a file "
                    f"({', '.join(with_files[:5])}); pass --files-root to read "
                    "files, or inline their text as pages"
                )
        try:
            result = import_bundle(
                records,
                files_root=options["files_root"],
                store_file=options["store_file"],
                import_batch=batch,
                deactivate_missing=options["deactivate_missing"],
                dry_run=options["dry_run"],
                strict=options["strict"],
            )
        except BundleError as exc:
            raise CommandError(f"Bundle refused: {exc}") from None
        for index, key, reason in result.skipped:
            self.stderr.write(
                self.style.WARNING(
                    f"Skipped record {index} ({key or 'no key'}): {reason}"
                )
            )
        for index, key, warning in result.warnings:
            self.stderr.write(self.style.WARNING(f"Record {index} ({key}): {warning}"))
        written = result.created + result.updated
        summary = (
            f"Plan document import done: created={result.created} "
            f"updated={result.updated} unchanged={result.unchanged} "
            f"skipped={len(result.skipped)} "
            f"deactivated={result.deactivated if options['deactivate_missing'] else 'n/a'} "
            f"dry_run={options['dry_run']}"
        )
        if result.skipped and not written and not result.unchanged:
            self.stdout.write(self.style.ERROR(summary))
        elif result.skipped or result.warnings:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

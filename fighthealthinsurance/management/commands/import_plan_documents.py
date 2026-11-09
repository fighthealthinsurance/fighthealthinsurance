"""Load a plan-document bundle into PublicPlanDocument (plan_document_bundle).

The bundle comes from the plan-document pipeline in
healthinsurance-llm-internal (``python -m dataset_tools.plan_document_export``
writes a zip). Read it from a file, or from stdin with ``--source -`` so it
can be piped into a pod without landing on disk::

    python manage.py import_plan_documents --source plan-docs.zip --dry-run
    python manage.py import_plan_documents --source bundle.json --dry-run
    kubectl exec -i <pod> -- python manage.py import_plan_documents --source - < plan-docs.zip

A zip (by its ``.zip`` name, its first bytes, or ``--format zip``) is read in
place: its ``bundle.json`` (or ``bundle.jsonl``) holds the records, its
``files/`` members are what their ``file`` fields name, and its
``SHA256SUMS``, when present, is checked before anything is written; it
takes no ``--files-root``. In a loose JSON bundle a record that names a
``file`` needs ``--files-root`` (so a piped JSON bundle carries its text
inline, as ``pages`` or ``text``). Records are checked one
by one: a refused record is skipped and counted, and the run exits 0 with
the summary styled as a warning; ``--strict`` refuses the whole bundle on
the first bad record instead. A bundle that cannot be read at all (not
JSON, too large, a key given twice) is a CommandError. ``--dry-run`` does
everything and rolls it back; ``--deactivate-missing`` retires the named
carriers' documents the bundle no longer lists.

Output names keys, fields and counts. It never prints a document's text.
"""

import datetime
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.plan_document_bundle import (
    MAX_BUNDLE_BYTES,
    BundleArchive,
    BundleDecodeError,
    BundleError,
    decode_bundle,
    import_bundle,
    looks_like_zip,
)

READ_CHUNK = 1 << 20  # 1 MiB


class Command(BaseCommand):
    help = (
        "Load a plan-document bundle (a zip, JSON or JSON Lines; a file or '-' "
        "for stdin)."
    )
    # Lets tests hand the command a stdin of their own.
    stealth_options = ("stdin",)

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--source",
            "-s",
            default="-",
            help=(
                "Path to the bundle (.zip, .json or .jsonl), or '-' (the default) "
                "to read it from stdin."
            ),
        )
        parser.add_argument(
            "--format",
            choices=("auto", "json", "jsonl", "zip"),
            default="auto",
            help=(
                "auto reads a .zip name or zip bytes as an archive, a leading '[' "
                "or '{' as JSON and anything else as JSON Lines."
            ),
        )
        parser.add_argument(
            "--files-root",
            default=None,
            help=(
                "Directory the records' relative 'file' paths are read under "
                "(not with an archive, which carries its own files)."
            ),
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

    def _load(
        self, source: str, fmt: str, stdin: Any
    ) -> Tuple[List[dict], Optional[BundleArchive]]:
        """The bundle's records, and the archive they came from when the
        source is a zip (open: the caller closes it)."""
        archive: Optional[BundleArchive] = None
        try:
            try:
                if source == "-":
                    raw = self._read(getattr(stdin, "buffer", stdin))
                    if fmt == "zip" or (fmt == "auto" and isinstance(raw, bytes)):
                        if not isinstance(raw, bytes):
                            raise CommandError(
                                "A zip bundle on stdin needs a binary stream"
                            )
                        if fmt == "zip" or looks_like_zip(raw):
                            archive = BundleArchive.open_bytes(raw)
                else:
                    path = Path(source)
                    if not path.is_file():
                        raise CommandError(f"No bundle file at {source}")
                    if path.stat().st_size > MAX_BUNDLE_BYTES:
                        raise CommandError(
                            f"The bundle is larger than {MAX_BUNDLE_BYTES} bytes"
                        )
                    with path.open("rb") as fh:
                        head = fh.read(4)
                    if fmt == "zip" or (
                        fmt == "auto"
                        and (source.lower().endswith(".zip") or looks_like_zip(head))
                    ):
                        archive = BundleArchive.open_path(str(path))
                    else:
                        with path.open("rb") as fh:
                            raw = self._read(fh)
                if archive is not None:
                    return archive.records(), archive
                text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            except UnicodeDecodeError:
                raise CommandError("The bundle is not UTF-8 text") from None
            if fmt == "auto":
                if source.lower().endswith(".jsonl"):
                    fmt = "jsonl"
                else:
                    head_char = text.lstrip()[:1]
                    fmt = "json" if head_char in ("[", "{") else "jsonl"
            try:
                return decode_bundle(text, fmt), None
            except BundleDecodeError as exc:
                raise CommandError(f"The bundle is {exc}") from None
        except BundleError as exc:
            if archive is not None:
                archive.close()
            raise CommandError(f"Bundle refused: {exc}") from None
        except BaseException:
            if archive is not None:
                archive.close()
            raise

    def handle(self, *args: Any, **options: Any) -> None:
        records, archive = self._load(
            options["source"], options["format"], options.get("stdin") or sys.stdin
        )
        try:
            self._import(records, archive, options)
        finally:
            if archive is not None:
                archive.close()

    def _import(
        self, records: List[dict], archive: Optional[BundleArchive], options: Any
    ) -> None:
        if archive is not None and options["files_root"] is not None:
            raise CommandError(
                "An archive carries its own files: --files-root is not used with one"
            )
        checksums = ""
        if archive is not None:
            try:
                verified = archive.verify_checksums()
            except BundleError as exc:
                raise CommandError(f"Bundle refused: {exc}") from None
            checksums = (
                f" (archive: {archive.file_count} file member(s), "
                f"{'checksums verified' if verified else 'no SHA256SUMS'})"
            )
        batch = options["batch"] or datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        self.stdout.write(
            f"Loaded {len(records)} record(s) from {options['source']!r}{checksums}"
        )
        if options["files_root"] is None and archive is None:
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
                files_root=archive if archive is not None else options["files_root"],
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

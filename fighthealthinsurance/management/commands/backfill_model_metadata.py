"""Backfill/normalize model metadata on historical generation records.

Older code stamped ``ProposedAppeal.model_name`` / ``ChooserCandidate.model_name``
with whatever ``str(model)`` produced at the time, which for unregistered
instances was an opaque object repr (``<...RemoteAnthropic object at 0x...>``)
or a ``ClassName(wire-model-id)`` descriptor, and occasionally the wire-level
model id instead of the friendly registry name. Those rows exist but never
line up with the registry names the usage dashboard aggregates, so the models
look absent from reporting.

This command rewrites ONLY values whose canonical friendly name can be
inferred unambiguously from the backend catalogs:

* ``ClassName(wire-id)``  -> the catalog entry for (backend class, wire id)
* ``<...ClassName object at 0x...>`` -> only when that backend class exposes
  exactly ONE catalog model (otherwise which model it was is unknowable)
* a bare wire id          -> only when exactly ONE catalog entry across all
  backends uses that internal name (e.g. "claude-sonnet-4-6" is ambiguous
  between anthropic/ and azure-anthropic/ and is left alone)

Rows with NULL model_name are genuinely unknown (heavy user edits, rows
predating the field) and are intentionally left for the dashboard's
"(unknown)" bucket. Anything else unrecognized is left untouched and counted.

Dry run by default; pass --apply to write changes. Reports counts for
repaired / already-canonical / left-unknown / ambiguous / unclassifiable.
"""

import re
from collections import Counter, defaultdict
from typing import Any, Dict, Optional, Set, Tuple

from django.core.management.base import BaseCommand
from django.db.models import Count as DjangoCount

# str(model) fallback used by RemoteModelLike.__str__ for unstamped instances.
_CLASS_MODEL_RE = re.compile(r"^(\w+)\((.+)\)$")
# Default object repr from before __str__/name stamping existed.
_OBJECT_REPR_RE = re.compile(r"^<(?:[\w.]+\.)*(\w+) object at 0x[0-9a-fA-F]+>$")

# Names that are already canonical but not part of any backend catalog.
_SPECIAL_NAMES = {"synthesized"}


class Command(BaseCommand):
    help = (
        "Normalize historical ProposedAppeal/ChooserCandidate model_name values "
        "(object reprs, ClassName(model) descriptors, bare wire ids) to the "
        "canonical registry names used by the ML usage dashboard. Only "
        "unambiguous mappings are applied; dry-run by default (use --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the repairs (default is a dry run that only reports).",
        )

    # --- catalog --------------------------------------------------------

    def _build_catalog(
        self,
    ) -> Tuple[
        Set[str], Dict[str, Set[str]], Dict[Tuple[str, str], str], Dict[str, Set[str]]
    ]:
        """Canonical name maps from every backend's ungated model_catalog().

        Returns (friendly_names, by_internal, by_class_and_internal, by_class).
        """
        from fighthealthinsurance.ml.ml_models import candidate_model_backends

        friendly: Set[str] = set(_SPECIAL_NAMES)
        by_internal: Dict[str, Set[str]] = defaultdict(set)
        by_class_and_internal: Dict[Tuple[str, str], str] = {}
        by_class: Dict[str, Set[str]] = defaultdict(set)

        for backend_cls in candidate_model_backends:
            try:
                catalog = backend_cls.model_catalog()
            except Exception:
                continue
            for desc in catalog:
                friendly.add(desc.name)
                by_internal[desc.internal_name].add(desc.name)
                by_class_and_internal[(backend_cls.__name__, desc.internal_name)] = (
                    desc.name
                )
                by_class[backend_cls.__name__].add(desc.name)
        return friendly, dict(by_internal), by_class_and_internal, dict(by_class)

    def _classify(
        self,
        value: str,
        friendly: Set[str],
        by_internal: Dict[str, Set[str]],
        by_class_and_internal: Dict[Tuple[str, str], str],
        by_class: Dict[str, Set[str]],
    ) -> Tuple[str, Optional[str]]:
        """Return (bucket, canonical_name_or_None) for a stored value.

        Buckets: already_ok, repaired, ambiguous, unclassifiable.
        """
        if value in friendly:
            return ("already_ok", None)

        m = _CLASS_MODEL_RE.match(value)
        if m and m.group(1) in by_class:
            canonical = by_class_and_internal.get((m.group(1), m.group(2)))
            if canonical:
                return ("repaired", canonical)
            return ("unclassifiable", None)

        m = _OBJECT_REPR_RE.match(value)
        if m and m.group(1) in by_class:
            names = by_class[m.group(1)]
            if len(names) == 1:
                return ("repaired", next(iter(names)))
            return ("ambiguous", None)

        names = by_internal.get(value, set())
        if len(names) == 1:
            return ("repaired", next(iter(names)))
        if len(names) > 1:
            return ("ambiguous", None)
        return ("unclassifiable", None)

    # --- main -----------------------------------------------------------

    def handle(self, *args: str, **options: Any):
        from fighthealthinsurance.models import ChooserCandidate, ProposedAppeal

        apply_changes: bool = options["apply"]
        friendly, by_internal, by_class_and_internal, by_class = self._build_catalog()

        totals: Counter = Counter()
        planned: Dict[str, Dict[str, str]] = {}  # table -> {old: new}

        for label, model_cls in (
            ("ProposedAppeal", ProposedAppeal),
            ("ChooserCandidate", ChooserCandidate),
        ):
            null_count = model_cls.objects.filter(model_name__isnull=True).count()
            totals[f"{label}.left_unknown_null"] = null_count

            renames: Dict[str, str] = {}
            # One aggregate query yields every distinct value WITH its row
            # count — avoids a COUNT() query per distinct model_name, which
            # would crawl on large ProposedAppeal/ChooserCandidate tables.
            value_counts = (
                model_cls.objects.filter(model_name__isnull=False)
                .order_by()
                .values_list("model_name")
                .annotate(c=DjangoCount("id"))
            )
            for value, count in value_counts:
                if value is None:  # guarded by the isnull filter; keeps mypy happy
                    continue
                bucket, canonical = self._classify(
                    value, friendly, by_internal, by_class_and_internal, by_class
                )
                totals[f"{label}.{bucket}"] += count
                if bucket == "repaired" and canonical:
                    renames[value] = canonical
                elif bucket in ("ambiguous", "unclassifiable"):
                    self.stdout.write(
                        f"  [{label}] {bucket}: {value!r} ({count} row(s)) — left as-is"
                    )
            planned[label] = renames

        if apply_changes:
            for label, model_cls in (
                ("ProposedAppeal", ProposedAppeal),
                ("ChooserCandidate", ChooserCandidate),
            ):
                for old, new in planned[label].items():
                    updated = model_cls.objects.filter(model_name=old).update(
                        model_name=new
                    )
                    self.stdout.write(
                        f"  [{label}] {old!r} -> {new!r}: {updated} row(s)"
                    )
        else:
            for label in planned:
                for old, new in planned[label].items():
                    self.stdout.write(f"  [{label}] would repair {old!r} -> {new!r}")

        mode = "APPLIED" if apply_changes else "DRY RUN (use --apply to write)"
        self.stdout.write(self.style.SUCCESS(f"Backfill {mode}. Summary:"))
        for table in ("ProposedAppeal", "ChooserCandidate"):
            repaired = totals[f"{table}.repaired"]
            ok = totals[f"{table}.already_ok"]
            unknown = totals[f"{table}.left_unknown_null"]
            ambiguous = totals[f"{table}.ambiguous"]
            unclassifiable = totals[f"{table}.unclassifiable"]
            self.stdout.write(
                f"  {table}: repaired={repaired} already_canonical={ok} "
                f"left_unknown(null)={unknown} ambiguous={ambiguous} "
                f"unclassifiable={unclassifiable}"
            )

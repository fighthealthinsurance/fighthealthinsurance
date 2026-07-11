"""Backfill and normalize historical model attribution for usage reporting.

Repairs the two data problems behind the ML model usage dashboard's bad rows:

* Chosen ``ProposedAppeal`` rows with ``model_name`` NULL (picks recorded
  before model tracking existed, or while the frontend id-echo was missing).
  Recovery, strictly evidence-based and in order:
    1. exact appeal_text match against a generated draft for the same denial;
    2. sole-draft inference (every draft for the denial came from one model,
       and the pick was not an arbitrary-text ``editted`` submission).
  Rows with no recoverable evidence are stamped ``legacy-unattributed`` —
  never guessed onto a current model. (Their generating model is simply not
  in the database: the drafts they were picked from predate the
  ``model_name`` column, so there is nothing to join back to.)

* ``ChooserCandidate`` rows whose ``model_name`` is a default Python object
  repr (``<...DeepInfra object at 0x7f81...>``), written before model
  instances had stable names. The configured model is unrecoverable (no
  related record captured a model identifier historically), so these
  normalize to ``legacy-unresolved (ClassName)`` — the class is the only
  stable information in the repr; the memory address is dropped so instances
  aggregate.

Safety properties:
  * Dry run by default; ``--apply`` is required to write.
  * Idempotent: repaired rows no longer match any repair condition, and
    ``legacy-unattributed`` rows are only re-touched if recovery newly
    succeeds (it never overwrites a valid canonical name).
  * Only pre-tracking chosen rows (``created_at`` NULL) are labeled; a chosen
    row's ``model_name`` is already final when it appears, so there is no
    live-write race to guard against.

``--audit`` prints a read-only data audit (totals, attribution gaps,
distinct labels, per-window counts, chosen>presented anomalies) without
changing anything.
"""

import datetime
from collections import Counter
from typing import Optional, Tuple

from django.core.management.base import BaseCommand
from django.db.models import Q, Value
from django.db.models.functions import Coalesce, Trim
from django.utils import timezone

from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    is_object_repr,
    normalize_model_label,
)
from fighthealthinsurance.models import ChooserCandidate, ChooserVote, ProposedAppeal


class Command(BaseCommand):
    help = (
        "Backfill/normalize historical ML model attribution used by the "
        "model-usage dashboard (dry run by default; --apply to write; "
        "--audit for a read-only data audit)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command only "
            "reports what it would do.",
        )
        parser.add_argument(
            "--audit",
            action="store_true",
            help="Print a read-only audit of model attribution data and exit.",
        )

    def handle(self, *args, **options):
        if options["audit"]:
            self._audit()
            return
        apply_changes: bool = options["apply"]
        mode = "APPLY" if apply_changes else "DRY RUN (pass --apply to write)"
        self.stdout.write(f"== Model usage attribution backfill: {mode} ==")
        self._backfill_proposed_appeals(apply_changes)
        self._backfill_chooser_candidates(apply_changes)
        if not apply_changes:
            self.stdout.write("Dry run complete; no rows were modified.")

    # ---------------------------------------------------------------- #
    # ProposedAppeal (denial flow picks)
    # ---------------------------------------------------------------- #

    def _recover_proposed(self, pa: ProposedAppeal) -> Optional[Tuple[str, bool, str]]:
        """Return (model_name, synthesized, method) recovered from related
        records, or None when the database lacks sufficient evidence."""
        if pa.for_denial_id is None:
            return None
        # model_name is blank=True, so exclude empty strings too — a blank
        # label is not usable evidence and must not be copied onto the pick.
        original = (
            ProposedAppeal.objects.filter(
                for_denial_id=pa.for_denial_id,
                chosen=False,
                appeal_text=pa.appeal_text,
                model_name__isnull=False,
            )
            .exclude(model_name="")
            .order_by("-id")
            .first()
        )
        if original is not None:
            original_name = original.model_name
            if (
                original_name
                and original_name.strip()
                and not is_object_repr(original_name)
            ):
                return (original_name, original.synthesized, "text_match")
        if not pa.editted:
            inferred = ProposedAppeal.sole_draft_attribution(pa.for_denial_id)
            if inferred is not None and not is_object_repr(inferred[0]):
                return (inferred[0], inferred[1], "sole_draft")
        return None

    def _backfill_proposed_appeals(self, apply_changes: bool) -> None:
        self.stdout.write("\n-- ProposedAppeal (chosen rows) --")
        total_chosen = ProposedAppeal.objects.filter(chosen=True).count()
        # Rows needing attention: missing attribution (NULL, empty, or
        # whitespace-only model_name), previously stamped as legacy (re-checked
        # in case drafts have since appeared), or possibly carrying an object
        # repr. Blank labels are matched with a trimmed annotation so that ""
        # and "  " are treated exactly like NULL (both mean "missing" once
        # normalize_model_label strips them). The ``startswith("<")`` clause is
        # a cheap SQL prefilter for object reprs (is_object_repr is a regex
        # that can't run in the DB); the loop below refines it, so a non-repr
        # value that merely starts with "<" is fetched but left untouched.
        targets = (
            ProposedAppeal.objects.filter(chosen=True)
            .annotate(_trimmed_model=Trim(Coalesce("model_name", Value(""))))
            .filter(
                Q(_trimmed_model="")  # NULL, empty, or whitespace-only
                | Q(model_name=LEGACY_UNATTRIBUTED_LABEL)
                | Q(model_name__startswith="<")
            )
        )
        target_total = targets.count()
        counts: Counter = Counter()
        for pa in targets.iterator():
            raw = pa.model_name
            raw_blank = raw is None or not raw.strip()
            recovered = self._recover_proposed(pa)
            new_name: Optional[str] = None
            new_synthesized: Optional[bool] = None
            if recovered is not None:
                new_name, new_synthesized, method = recovered
                counts[f"recovered_{method}"] += 1
            elif not raw_blank and is_object_repr(raw):
                new_name = normalize_model_label(raw)
                counts["normalized_repr"] += 1
            elif not raw_blank:
                # A non-blank label that is neither an object repr nor
                # recoverable: the legacy-unattributed marker itself, or a
                # malformed/hand-entered value that merely starts with "<".
                # Leave it exactly as-is — never relabel a value we don't
                # understand.
                counts["left_unchanged"] += 1
                continue
            elif pa.created_at is None:
                # Missing model_name (NULL/empty/whitespace) on a pre-tracking
                # pick (the column didn't exist yet): its drafts can't be
                # joined back, so it is genuinely unrecoverable. Label it
                # rather than guess.
                new_name = LEGACY_UNATTRIBUTED_LABEL
                counts["labeled_legacy_unattributed"] += 1
            elif raw is None:
                # Already-NULL post-tracking pick (e.g. several models in play
                # and the draft was edited away). Leave it NULL so it reports
                # as "(unattributed)" and stays recoverable if matching drafts
                # appear later.
                counts["left_unattributed"] += 1
                continue
            else:
                # Blank (empty/whitespace) post-tracking pick: collapse the
                # blank to NULL, the canonical "missing" value, so it reports
                # as "(unattributed)" consistently with NULL rows.
                new_name = None
                counts["normalized_blank_to_null"] += 1
            if new_name == raw and (
                new_synthesized is None or new_synthesized == pa.synthesized
            ):
                counts["already_consistent"] += 1
                continue
            counts["rows_changed"] += 1
            if apply_changes:
                pa.model_name = new_name
                update_fields = ["model_name"]
                if new_synthesized is not None and new_synthesized != pa.synthesized:
                    pa.synthesized = new_synthesized
                    update_fields.append("synthesized")
                pa.save(update_fields=update_fields)

        already_valid = total_chosen - target_total
        self.stdout.write(f"chosen rows total:                    {total_chosen}")
        self.stdout.write(f"already valid:                        {already_valid}")
        self.stdout.write(
            f"recovered via exact draft text match: {counts['recovered_text_match']}"
        )
        self.stdout.write(
            f"recovered via sole-draft inference:   {counts['recovered_sole_draft']}"
        )
        self.stdout.write(
            f"normalized from object reprs:         {counts['normalized_repr']}"
        )
        self.stdout.write(
            f"unrecoverable -> {LEGACY_UNATTRIBUTED_LABEL}:    "
            f"{counts['labeled_legacy_unattributed']}"
        )
        self.stdout.write(
            f"left NULL -> (unattributed):          {counts['left_unattributed']}"
        )
        self.stdout.write(
            f"blank -> NULL (unattributed):         {counts['normalized_blank_to_null']}"
        )
        self.stdout.write(
            f"left unchanged (already labeled/other): {counts['left_unchanged']}"
        )
        verb = "changed" if apply_changes else "would change"
        self.stdout.write(f"rows {verb}: {counts['rows_changed']}")

    # ---------------------------------------------------------------- #
    # ChooserCandidate (appeal_letter + chat_response)
    # ---------------------------------------------------------------- #

    def _backfill_chooser_candidates(self, apply_changes: bool) -> None:
        self.stdout.write("\n-- ChooserCandidate --")
        total = ChooserCandidate.objects.count()
        targets = ChooserCandidate.objects.filter(model_name__startswith="<")
        counts: Counter = Counter()
        distinct_before = set()
        distinct_after = set()
        for cand in targets.iterator():
            raw = cand.model_name
            if not is_object_repr(raw):
                # "<" prefix but not a default repr; leave it alone rather
                # than mangling a name we don't understand.
                counts["skipped_unrecognized"] += 1
                continue
            # No related data records which configured model produced a
            # repr-era candidate, so the class name inside the repr is the
            # only stable evidence. Normalize to it; never guess a model.
            distinct_before.add(raw)
            new_name = normalize_model_label(raw)
            distinct_after.add(new_name)
            counts["rows_changed"] += 1
            if apply_changes and new_name:
                cand.model_name = new_name
                cand.save(update_fields=["model_name"])

        self.stdout.write(f"candidates total:                {total}")
        self.stdout.write(
            f"already valid:                   {total - counts['rows_changed'] - counts['skipped_unrecognized']}"
        )
        self.stdout.write(f"normalized to legacy-unresolved: {counts['rows_changed']}")
        if counts["skipped_unrecognized"]:
            self.stdout.write(
                f"skipped (unrecognized '<' name): {counts['skipped_unrecognized']}"
            )
        verb = "changed" if apply_changes else "would change"
        self.stdout.write(f"rows {verb}: {counts['rows_changed']}")
        if distinct_before:
            self.stdout.write(
                f"distinct object-repr values collapsed: {len(distinct_before)} "
                f"-> {len(distinct_after)} labels: {sorted(x for x in distinct_after if x)}"
            )

    # ---------------------------------------------------------------- #
    # Read-only audit
    # ---------------------------------------------------------------- #

    def _audit(self) -> None:
        # Imported here: staff_views pulls in ray and the wider view stack,
        # which the backfill paths above don't need.
        from fighthealthinsurance.staff_views import ModelUsageDashboardView

        now = timezone.now()
        windows = [
            ("All Time", None),
            ("Last 1 Day", now - datetime.timedelta(days=1)),
            ("Last 7 Days", now - datetime.timedelta(days=7)),
            ("Last 30 Days", now - datetime.timedelta(days=30)),
        ]
        self.stdout.write("== Model usage attribution audit ==")

        self.stdout.write("\n-- ProposedAppeal --")
        qs = ProposedAppeal.objects
        total = qs.count()
        chosen_total = qs.filter(chosen=True).count()
        drafts_named = qs.filter(chosen=False, model_name__isnull=False).count()
        drafts_unnamed = qs.filter(chosen=False, model_name__isnull=True).count()
        chosen_null = qs.filter(chosen=True, model_name__isnull=True)
        chosen_null_legacy = chosen_null.filter(created_at__isnull=True).count()
        chosen_null_recent = chosen_null.filter(created_at__isnull=False).count()
        chosen_labeled_legacy = qs.filter(
            chosen=True, model_name=LEGACY_UNATTRIBUTED_LABEL
        ).count()
        created_null = qs.filter(created_at__isnull=True).count()
        self.stdout.write(f"rows total: {total} (chosen {chosen_total})")
        self.stdout.write(
            f"drafts (chosen=False): {drafts_named} with model_name, "
            f"{drafts_unnamed} without (pre-tracking; excluded from presented "
            f"so no denominator is fabricated)"
        )
        self.stdout.write(
            f"chosen rows missing attribution: {chosen_null.count()} "
            f"({chosen_null_legacy} pre-tracking/created_at NULL, "
            f"{chosen_null_recent} post-tracking) + "
            f"{chosen_labeled_legacy} already labeled {LEGACY_UNATTRIBUTED_LABEL}"
        )
        self.stdout.write(
            f"rows with created_at NULL (only visible in All Time): {created_null}"
        )
        recoverable = 0
        for pa in chosen_null.iterator():
            if self._recover_proposed(pa) is not None:
                recoverable += 1
        self.stdout.write(
            f"of the unattributed chosen rows, recoverable from related "
            f"records: {recoverable}; unrecoverable (drafts predate "
            f"model_name tracking or were never saved): "
            f"{chosen_null.count() - recoverable}"
        )

        for kind in ("appeal_letter", "chat_response"):
            self.stdout.write(f"\n-- ChooserCandidate/ChooserVote ({kind}) --")
            cands = ChooserCandidate.objects.filter(kind=kind)
            votes = ChooserVote.objects.filter(chosen_candidate__kind=kind)
            # order_by() clears ChooserCandidate's default ordering, which
            # would otherwise leak into the DISTINCT and make it per-row.
            distinct = list(
                cands.order_by().values_list("model_name", flat=True).distinct()
            )
            repr_cands = [v for v in distinct if is_object_repr(v)]
            self.stdout.write(
                f"candidates: {cands.count()}, votes: {votes.count()}, "
                f"distinct model_name values: {len(distinct)} "
                f"({len(repr_cands)} are object reprs)"
            )
            # Drop labels that normalize to None (blank/whitespace) rather
            # than stringifying them into a misleading literal "None".
            normalized = sorted(
                {
                    label
                    for v in distinct
                    if (label := normalize_model_label(v)) is not None
                }
            )
            self.stdout.write(f"distinct labels after normalization: {normalized}")

        self.stdout.write("\n-- Dashboard aggregates by window --")
        for label, since in windows:
            proposed = ModelUsageDashboardView._proposed_appeal_stats(since)
            appeal = ModelUsageDashboardView._chooser_stats("appeal_letter", since)
            chat = ModelUsageDashboardView._chooser_stats("chat_response", since)
            self.stdout.write(f"[{label}]")
            for source_name, rows in (
                ("ProposedAppeal", proposed),
                ("Chooser-Appeal", appeal),
                ("Chooser-Chat", chat),
            ):
                total_chosen = sum(r["chosen"] for r in rows)
                total_presented = sum(r["presented"] for r in rows)
                self.stdout.write(
                    f"  {source_name}: models={len(rows)} "
                    f"chosen={total_chosen} presented={total_presented}"
                )
                for r in rows:
                    if "object at 0x" in r["model_name"]:
                        self.stdout.write(
                            self.style.ERROR(f"    REPR LEAK: {r['model_name']!r}")
                        )
                    if r["presented"] and r["chosen"] > r["presented"]:
                        self.stdout.write(
                            f"    chosen>presented for {r['model_name']}: "
                            f"{r['chosen']}>{r['presented']} (multiple picks "
                            f"recorded per denial, e.g. re-submits)"
                        )
        self.stdout.write("\nAudit complete (read-only; no rows modified).")

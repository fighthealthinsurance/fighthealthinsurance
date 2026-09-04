"""Re-run the appeal fingerprint backfill after a deployment settles.

Migration 0202 runs the first pass, but it can race writer processes still
on pre-fingerprint code during the rollout (external review). The
post-rollout Job (k8s/temporal/backfill-fingerprints-job.yaml, applied by
scripts/build.sh only AFTER every writer Deployment and the Ray cluster have
finished rolling) runs this command with ``--strict``::

    python manage.py backfill_appeal_fingerprints --strict

Strict mode runs the backfill twice back to back and then an integrity
pass, and FAILS (non-zero exit) if:

* the second fill pass still filled rows, lost races, or left NULL rows it
  never classified (a pre-fingerprint writer inserted behind the scan), or
* the integrity pass had to re-key any row whose fingerprint no longer
  matched its current text (a pre-fingerprint writer EDITED text under a
  stale fingerprint -- NULL checks alone cannot see that; external review).

The Job's backoff then retries until a run comes back quiet. Without
``--strict`` it is a plain idempotent fill pass that prints counters; safe
to run any number of times.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.appeal_fingerprints import (
    REKEYED,
    run_backfill,
    verify_fingerprints,
)
from fighthealthinsurance.models import ProposedAppeal


class Command(BaseCommand):
    help = "Idempotently backfill ProposedAppeal.text_fingerprint (see 0202)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Run two fill passes plus an integrity pass and exit non-zero "
                "if a pre-fingerprint writer is evidently still active."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        first = run_backfill(ProposedAppeal)
        self.stdout.write(f"pass 1: {first}")
        if not options.get("strict"):
            return
        second = run_backfill(ProposedAppeal)
        self.stdout.write(f"pass 2: {second}")
        # Quiescent means: the pass wrote nothing, lost no races, AND every
        # NULL row still present is one the pass itself classified (known
        # duplicate or empty). A NULL row inserted after the pass took its
        # snapshot shows up only in remaining_null -- that is a live
        # pre-fingerprint writer, and must fail strict mode too (review).
        accounted = second["skipped_duplicate"] + second["skipped_empty"]
        unaccounted = second["remaining_null"] - accounted
        if second["filled"] or second["lost_race"] or unaccounted > 0:
            raise CommandError(
                "fingerprint backfill not quiescent: pass 2 filled "
                f"{second['filled']}, lost {second['lost_race']} races, and "
                f"{max(unaccounted, 0)} NULL row(s) appeared unclassified -- a "
                "writer on pre-fingerprint code is still running; retry after "
                "the rollout finishes draining old pods"
            )
        verified = verify_fingerprints(ProposedAppeal)
        self.stdout.write(f"verify: {verified}")
        if verified[REKEYED]:
            raise CommandError(
                f"fingerprint invariant violated: {verified[REKEYED]} row(s) "
                "carried a fingerprint that did not match their current text "
                "(re-keyed now) -- a writer on pre-fingerprint code edited "
                "appeal text; retry after the rollout finishes draining old pods"
            )
        self.stdout.write(
            f"quiescent: {second['remaining_null']} NULL row(s) remain (all "
            "known duplicates or empty legacy rows); "
            f"{verified['checked']} fingerprinted row(s) verified"
        )

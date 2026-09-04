"""Re-run the appeal fingerprint backfill after a deployment settles.

Migration 0202 runs the first pass, but it can race writer processes still
on pre-fingerprint code during the rollout (external review). The
post-rollout Job (k8s/temporal/backfill-fingerprints-job.yaml, applied by
scripts/build.sh) runs this command with ``--strict``::

    python manage.py backfill_appeal_fingerprints --strict

Strict mode runs the backfill twice back to back and FAILS (non-zero exit)
if the second pass still found rows to fill or lost a race -- proof that a
pre-fingerprint writer is still active. The Job's backoff then retries
until a pass comes back quiet, which is the enforceable version of "after
old writers drain". Without ``--strict`` it is a plain idempotent pass
that prints counters; safe to run any number of times.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance.appeal_fingerprints import run_backfill
from fighthealthinsurance.models import ProposedAppeal


class Command(BaseCommand):
    help = "Idempotently backfill ProposedAppeal.text_fingerprint (see 0202)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Run two passes and exit non-zero if the second still fills "
                "rows or loses races (a pre-fingerprint writer is still active)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        first = run_backfill(ProposedAppeal)
        self.stdout.write(f"pass 1: {first}")
        if not options.get("strict"):
            return
        second = run_backfill(ProposedAppeal)
        self.stdout.write(f"pass 2: {second}")
        if second["filled"] or second["lost_race"]:
            raise CommandError(
                "fingerprint backfill not quiescent: pass 2 filled "
                f"{second['filled']} and lost {second['lost_race']} races -- a "
                "writer on pre-fingerprint code is still running; retry after "
                "the rollout finishes draining old pods"
            )
        self.stdout.write(
            f"quiescent: {second['remaining_null']} NULL row(s) remain, all "
            "known duplicates or empty legacy rows"
        )

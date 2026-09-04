"""Re-run the appeal fingerprint backfill after a deployment settles.

Migration 0202 runs the first pass, but it can race writer processes still
on pre-fingerprint code during the rollout (external review). Once every
old writer is gone, run::

    python manage.py backfill_appeal_fingerprints

and check the printed counts: after this pass, ``remaining_null`` should
equal ``skipped_duplicate + skipped_empty + lost_race`` accumulated across
runs -- i.e. every remaining NULL is a known historical duplicate (or an
empty legacy row), which is what journey counting assumes. Idempotent;
safe to run any number of times.
"""

from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.appeal_fingerprints import run_backfill
from fighthealthinsurance.models import ProposedAppeal


class Command(BaseCommand):
    help = "Idempotently backfill ProposedAppeal.text_fingerprint (see 0202)."

    def handle(self, *args: Any, **options: Any) -> None:
        counts = run_backfill(ProposedAppeal)
        self.stdout.write(str(counts))

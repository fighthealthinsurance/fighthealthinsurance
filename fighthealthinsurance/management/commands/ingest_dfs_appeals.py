"""Ingest NY DFS External Appeals decisions from a CSV export.

The NY DFS exposes external appeals through a search UI; ingest a CSV that has
been exported (or scraped into CSV form) using the same loader as the CA DMHC
dataset. Pass ``--url`` or ``--file``; idempotent on ``(source, case_id)``.
"""

from fighthealthinsurance.management.commands._imr_ingest_base import IMRIngestCommand
from fighthealthinsurance.models import IMRDecision


class Command(IMRIngestCommand):
    help = "Ingest New York DFS External Appeals decisions from a CSV."
    source = IMRDecision.SOURCE_NY_DFS
    label = "NY DFS appeals"

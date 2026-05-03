"""Ingest CA DMHC Independent Medical Review decisions from the CHHS CSV export.

The CHHS open data portal hosts the IMR Determinations Trend dataset as a CSV.
Pass ``--url`` to download from a URL or ``--file`` to load a local CSV. The
loader is idempotent on ``(source, case_id)`` so re-running it refreshes the
existing rows.
"""

from fighthealthinsurance.management.commands._imr_ingest_base import IMRIngestCommand
from fighthealthinsurance.models import IMRDecision


class Command(IMRIngestCommand):
    help = "Ingest California DMHC Independent Medical Review decisions from a CSV."
    source = IMRDecision.SOURCE_CA_DMHC
    label = "CA DMHC IMR"

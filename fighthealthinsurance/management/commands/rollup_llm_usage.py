"""Roll up the LLM usage metrics' closed weeks of keyed network rows into
keyless summaries, delete the keyed rows, and apply the usage tables'
retention (ml/llm_usage_ledger.py). The hourly sweep
(sweep_assistant_drafts) runs the same thing; this is the manual entry
point::

    python manage.py rollup_llm_usage

Logs only counts.
"""

from typing import Any

from django.core.management.base import BaseCommand
from loguru import logger

from fighthealthinsurance.ml import llm_usage_ledger


class Command(BaseCommand):
    help = "Roll up and delete closed weeks of LLM usage network keys."

    def handle(self, *args: Any, **options: Any) -> None:
        result = llm_usage_ledger.rollup_and_sweep()
        logger.info(f"rollup_llm_usage: {result}")
        self.stdout.write(str(result))

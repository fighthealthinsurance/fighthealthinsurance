"""Delete assistant drafts past their expiry (assistant_drafts.py), with the
terms page's past days of per-address counts (assistant_ip_limit.py) and its
expired or revoked continue links (assistant_continue.py). Then roll up the
LLM usage metrics' closed weeks of keyed network rows and delete them, and
apply the usage tables' retention (ml/llm_usage_ledger.py); that runs last,
so a failure there still fails the job after the rest is done.

Hourly as its own CronJob (k8s/assistant-drafts-sweep-cronjob.yaml), and the
manual entry point::

    python manage.py sweep_assistant_drafts

It runs whatever the flags say and logs only counts.
"""

from typing import Any

from django.core.management.base import BaseCommand
from loguru import logger

from fighthealthinsurance import assistant_continue, assistant_ip_limit
from fighthealthinsurance.assistant_drafts import sweep_expired
from fighthealthinsurance.ml import llm_usage_ledger


class Command(BaseCommand):
    help = "Delete assistant drafts (the MCP chat path) past their expiry."

    def handle(self, *args: Any, **options: Any) -> None:
        deleted = sweep_expired()
        days = assistant_ip_limit.sweep_old()
        links = assistant_continue.sweep_expired()
        logger.info(
            f"sweep_assistant_drafts: deleted {deleted} expired draft(s), "
            f"{days} past per-address count(s), {links} continue link(s)"
        )
        usage = llm_usage_ledger.rollup_and_sweep()
        logger.info(f"sweep_assistant_drafts: LLM usage {usage}")

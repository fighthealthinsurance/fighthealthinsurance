"""Delete assistant drafts past their expiry (assistant_drafts.py).

Hourly as its own CronJob (k8s/assistant-drafts-sweep-cronjob.yaml), and the
manual entry point::

    python manage.py sweep_assistant_drafts

It runs whatever the flags say and logs only a count.
"""

from typing import Any

from django.core.management.base import BaseCommand
from loguru import logger

from fighthealthinsurance.assistant_drafts import sweep_expired


class Command(BaseCommand):
    help = "Delete assistant drafts (the MCP chat path) past their expiry."

    def handle(self, *args: Any, **options: Any) -> None:
        deleted = sweep_expired()
        logger.info(f"sweep_assistant_drafts: deleted {deleted} expired draft(s)")

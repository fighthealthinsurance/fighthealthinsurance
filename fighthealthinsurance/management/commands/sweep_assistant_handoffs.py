"""Delete assistant handoffs past their 2 hours (assistant_handoff.py).

Every prepare_appeal call deletes expired rows first; this is the backstop,
for when no one calls it. It runs every 10 minutes as its own CronJob
(k8s/assistant-handoff-sweep-cronjob.yaml) and is also the manual entry
point::

    python manage.py sweep_assistant_handoffs

It runs whatever the flags say, so turning prepare_appeal off still empties
the table. It logs only a count.
"""

from typing import Any

from django.core.management.base import BaseCommand
from loguru import logger

from fighthealthinsurance.assistant_handoff import sweep_expired


class Command(BaseCommand):
    help = "Delete assistant handoffs (prepare_appeal links) past their expiry."

    def handle(self, *args: Any, **options: Any) -> None:
        deleted = sweep_expired()
        logger.info(f"sweep_assistant_handoffs: deleted {deleted} expired handoff(s)")

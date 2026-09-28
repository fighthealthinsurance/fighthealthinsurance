"""Bring FHI's Temporal Schedules in step with their flags.

Today there is one, ``chat-routing-policy``: created or updated (and
unpaused) when TEMPORAL_ENABLED and TEMPORAL_CHAT_POLICY_ENABLED are both on,
paused when not. The appeal-worker process does the same at start-up, so
this command is for doing it by hand, for example after changing the flag
without restarting the workers. It is idempotent.

With TEMPORAL_ENABLED off it does nothing and does not connect.
"""

import asyncio
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create, update or pause FHI's Temporal Schedules to match their flags."

    def handle(self, *args: Any, **options: Any) -> None:
        if not getattr(settings, "TEMPORAL_ENABLED", False):
            self.stdout.write("TEMPORAL_ENABLED is off; not contacting Temporal.")
            return
        from fighthealthinsurance.temporal_client import CHAT_POLICY_SCHEDULE_ID

        outcome = asyncio.run(self._ensure())
        self.stdout.write(f"{CHAT_POLICY_SCHEDULE_ID}: {outcome}")

    async def _ensure(self) -> str:
        from fighthealthinsurance.temporal_client import (
            chat_policy_schedule_enabled,
            ensure_chat_policy_schedule,
            get_temporal_client,
        )

        client = await get_temporal_client()
        return await ensure_chat_policy_schedule(
            client, enabled=chat_policy_schedule_enabled()
        )

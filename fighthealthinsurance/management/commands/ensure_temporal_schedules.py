"""Bring FHI's Temporal Schedules in step with their flags.

There are two. ``chat-routing-policy`` is created or updated (and
unpaused) when TEMPORAL_ENABLED and TEMPORAL_CHAT_POLICY_ENABLED are both
on, paused when not. ``plan-documents-sweep`` follows
temporal_client.plan_documents_enabled() the same way. The appeal-worker
process does the same at start-up, so this command is for doing it by hand,
for example after changing a flag without restarting the workers. It is
idempotent.

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
        for schedule_id, outcome in asyncio.run(self._ensure()):
            self.stdout.write(f"{schedule_id}: {outcome}")

    async def _ensure(self) -> list[tuple[str, str]]:
        from fighthealthinsurance.temporal_client import (
            CHAT_POLICY_SCHEDULE_ID,
            PLAN_DOCUMENTS_SCHEDULE_ID,
            chat_policy_schedule_enabled,
            ensure_chat_policy_schedule,
            ensure_plan_documents_schedule,
            get_temporal_client,
            plan_documents_enabled,
        )

        client = await get_temporal_client()
        policy = await ensure_chat_policy_schedule(
            client, enabled=chat_policy_schedule_enabled()
        )
        plan_documents = await ensure_plan_documents_schedule(
            client, enabled=plan_documents_enabled()
        )
        return [
            (CHAT_POLICY_SCHEDULE_ID, policy),
            (PLAN_DOCUMENTS_SCHEDULE_ID, plan_documents),
        ]

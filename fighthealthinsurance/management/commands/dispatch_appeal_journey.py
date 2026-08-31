"""Dispatch a durable appeal-generation journey for one denial.

Manual/testing entry point for ``GenerateAppealWorkflow`` while no product
surface dispatches it yet: look the denial up by uuid, then hand its opaque
identifiers to Temporal. Requires TEMPORAL_ENABLED and
TEMPORAL_APPEAL_JOURNEY_ENABLED plus a running worker
(``python manage.py run_temporal_worker``).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Start a durable GenerateAppealWorkflow for a denial (by uuid)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("denial_uuid", help="The Denial uuid to generate for.")

    def handle(self, *args: Any, **options: Any) -> None:
        from fighthealthinsurance.models import Denial
        from fighthealthinsurance.temporal_client import dispatch_appeal_generation

        denial_uuid = options["denial_uuid"]
        try:
            denial = Denial.objects.get(uuid=denial_uuid)
        except Denial.DoesNotExist:
            raise CommandError(f"No denial with uuid {denial_uuid}")

        if dispatch_appeal_generation(denial.hashed_email, str(denial.uuid)):
            self.stdout.write(f"Dispatched appeal journey for denial {denial_uuid}")
        else:
            raise CommandError(
                "Dispatch failed -- is TEMPORAL_ENABLED (and "
                "TEMPORAL_APPEAL_JOURNEY_ENABLED) set, with Temporal reachable?"
            )

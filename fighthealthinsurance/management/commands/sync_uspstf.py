"""Refresh the local USPSTF recommendation cache from the public API."""

import asyncio
from typing import Any

from django.core.management.base import BaseCommand

from fighthealthinsurance.uspstf_api import USPSTFClient


class Command(BaseCommand):
    help = (
        "Fetch USPSTF Prevention TaskForce recommendations and store them in the "
        "local USPSTFRecommendation cache. Falls back to the bundled dataset when "
        "the API is unreachable."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--api-url",
            dest="api_url",
            default=None,
            help="Override the USPSTF API URL (defaults to USPSTF_API_URL env or built-in default).",
        )
        parser.add_argument(
            "--timeout",
            dest="timeout",
            type=float,
            default=30.0,
            help="HTTP timeout in seconds (default: 30).",
        )

    def handle(self, *args: str, **options: Any) -> None:
        client = USPSTFClient(
            api_url=options.get("api_url"), timeout=options["timeout"]
        )
        count = asyncio.run(client.sync_recommendations())
        self.stdout.write(self.style.SUCCESS(f"Synced {count} USPSTF recommendations."))

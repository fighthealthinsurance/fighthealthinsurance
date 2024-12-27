from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Launch the polling actors"

    def handle(self, *args: str, **options: Any):
        from fighthealthinsurance.polling_actor_setup import epar, fpar
        print(f"Loaded actor {epar} {fpar}")

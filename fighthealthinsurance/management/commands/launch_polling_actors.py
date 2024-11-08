import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Launch the polling actors"

    def handle(self, *args, **options):
        from fighthealthinsurance.polling_actor_setup import epar, fpar
